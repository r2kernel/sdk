// Internal immutable publications. Readers borrow a closure; management owns
// every node and performs published closure destruction. Retired ownership grows only
// with generations that still have active readers, not publication history.
class ControlHandlerPublication {
    static unsigned& packet_invocation_depth() {
        thread_local unsigned depth = 0;
        return depth;
    }
    struct PacketInvocation {
        PacketInvocation() { ++packet_invocation_depth(); }
        ~PacketInvocation() { --packet_invocation_depth(); }
    };
    struct Node {
        explicit Node(const r2kernel::wrapper::ControlHandler& value) : handler(value) {}
        r2kernel::wrapper::ControlHandler handler;
        std::size_t readers = 0;
        std::unique_ptr<Node> next;
    };
public:
    class Reader {
    public:
        Reader(ControlHandlerPublication& owner, Node* node) : owner_(owner), node_(node) {}
        Reader(const Reader&) = delete;
        Reader& operator=(const Reader&) = delete;
        ~Reader() {
            if (!node_) return;
            std::lock_guard<std::mutex> guard(owner_.mutex_);
            --node_->readers;
        }
        explicit operator bool() const { return node_ && static_cast<bool>(node_->handler); }
        r2kernel::wrapper::ControlResponse operator()(r2kernel::wrapper::ControlRequest request) const {
            PacketInvocation packet;
            // The public handler contract copies mutable by-value captures for
            // each request. Keep that clone, outside all publication/Impl locks.
            // This short-lived clone has its existing packet-thread lifetime;
            // the immutable published original is retained by management.
            auto invocation = node_->handler;
            return invocation(std::move(request));
        }
    private:
        ControlHandlerPublication& owner_;
        Node* node_;
    };
    Reader acquire() {
        std::lock_guard<std::mutex> guard(mutex_);
        if (current_) ++current_->readers;
        return Reader(*this, current_.get());
    }
    void publish(const r2kernel::wrapper::ControlHandler& handler) {
        auto prepared = std::make_unique<Node>(handler);
        std::unique_ptr<Node> garbage;
        {
            std::lock_guard<std::mutex> guard(mutex_);
            if (current_) {
                current_->next = std::move(retired_);
                retired_ = std::move(current_);
            }
            current_ = std::move(prepared);
            // Public registration may be called recursively by a packet
            // callback. That thread only publishes; the existing accept
            // management tick/stop later destroys inactive originals.
            if (packet_invocation_depth() == 0) collect_inactive(garbage);
        }
        destroy_nodes(std::move(garbage));
    }
    // Called by existing management work if publication is otherwise idle.
    void collect() {
        if (packet_invocation_depth() != 0) return;
        std::unique_ptr<Node> garbage;
        {
            std::lock_guard<std::mutex> guard(mutex_);
            collect_inactive(garbage);
        }
        destroy_nodes(std::move(garbage));
    }
    void clear() {
        std::unique_ptr<Node> garbage;
        {
            std::lock_guard<std::mutex> guard(mutex_);
            if (current_ && current_->readers != 0)
                throw std::runtime_error("Control handler clear requires drained readers");
            for (auto* node = retired_.get(); node; node = node->next.get())
                if (node->readers != 0) throw std::runtime_error("Control handler clear requires drained readers");
            garbage = std::move(retired_);
            if (current_) {
                current_->next = std::move(garbage);
                garbage = std::move(current_);
            }
        }
        destroy_nodes(std::move(garbage));
    }
    ~ControlHandlerPublication() { clear(); }
private:
    void collect_inactive(std::unique_ptr<Node>& garbage) {
        auto* link = &retired_;
        while (*link) {
            if ((*link)->readers != 0) { link = &(*link)->next; continue; }
            auto removed = std::move(*link);
            *link = std::move(removed->next);
            removed->next = std::move(garbage);
            garbage = std::move(removed);
        }
    }
    static void destroy_nodes(std::unique_ptr<Node> nodes) {
        // Iteration bounds the destructor stack even with many active handlers.
        while (nodes) {
            auto removed = std::move(nodes);
            nodes = std::move(removed->next);
        }
    }
    std::mutex mutex_;
    std::unique_ptr<Node> current_, retired_;
};
