// Included at the end of client.cpp (same translation unit). Cold entry points
// of the Control registration upkeep; the keeper and the registration request
// live in control_registration.cpp.

#if defined(__GNUC__)
#define R2KERNEL_SDK_COLD __attribute__((cold, noinline))
#else
#define R2KERNEL_SDK_COLD
#endif

namespace r2kernel::core {

// One registration attempt with the unchanged session, PID, lease and notify
// endpoint after Control lost this client's registration. Throws on refusal.
// Settings that per-call paths read without a lock (Event pool, inline
// endpoint, Control payload threshold and shared buffer pool) are kept; the
// returned text names those Control now offers differently (they need a
// subsystem restart).
R2KERNEL_SDK_COLD std::string R2KernelClient::Impl::reregister_subsystem() {
    if (down_reported.load()) {
        throw std::runtime_error("the subsystem already reported itself down; it does not register again");
    }
    const std::string response = exchange_control_json(detail::registration_request(options, notify_endpoint),
        options.control_host, options.control_udp_port, startup_request_timeout_ms);
    if (json_string_field(response, kResult).value_or("") != kOk) {
        throw std::runtime_error("Control refused the registration: " + json_string_field(response, "error").value_or(response));
    }
    apply_startup_barrier_config(response);
    apply_clock_status_from_registration(response);
    if (const auto control_endpoint = json_string_field(response, kLocalControlEndpoint)) {
        std::lock_guard<std::mutex> route_guard(mutex);
        if (local_control_endpoint != *control_endpoint) {
            local_control_endpoint = *control_endpoint;
#if !defined(_WIN32)
            control_shm_endpoint_generation.fetch_add(1, std::memory_order_release);
#endif
        }
    }
#if !defined(_WIN32)
    configure_manager_peer_auth(response);
    configure_control_shm(response);
#endif
    std::string changed;
    const auto note = [&changed](bool differs, const char* name) {
        if (differs) changed += (changed.empty() ? "" : ", ") + std::string(name);
    };
    const bool pool_differs = json_string_field(response, kPoolName).value_or("") != shm_name
        || json_int_field(response, kPoolSlotSize).value_or(-1) != static_cast<long long>(slot_size)
        || json_int_field(response, kPoolSlotCount).value_or(-1) != static_cast<long long>(slot_count)
        || detail::shm_identity(shm_name) != event_pool_identity;
    note(pool_differs, "Event shared-memory pool");
    event_pool_replaced = event_pool_replaced || pool_differs;
    if (const auto endpoint = json_string_field(response, kEventInlineEndpoint))
        note(detail::registered_event_inline_endpoint(*endpoint) != event_inline_endpoint, "Event inline endpoint");
    return changed;
}

// Requests every accepted Event publication and subscription again, one
// attempt each, on the new registration. Throws on the first refusal.
R2KERNEL_SDK_COLD std::size_t R2KernelClient::Impl::restore_event_registrations() {
    std::vector<r2kernel::wrapper::EventPublishSpec> publications;
    std::vector<r2kernel::wrapper::EventSubscribeSpec> subscriptions;
    {
        std::lock_guard<std::mutex> guard(mutex);
        for (const auto& entry : publish_specs) publications.push_back(entry.second);
        for (const auto& entry : subscribe_specs) subscriptions.push_back(entry.second);
    }
    if (publications.empty() && subscriptions.empty()) return 0;
    if (event_pool_replaced) {
        throw EventPoolReplacedError(
            "Control serves a different Event shared-memory pool than the one this process mapped; this "
            "process's Event publications and subscriptions cannot be restored until it restarts");
    }
    for (const auto& spec : publications)
        if (!register_publish(spec, 1)) throw std::runtime_error("Control refused the Event publication " + spec.topic);
    for (const auto& spec : subscriptions)
        if (!register_subscribe(spec, 1, true))
            throw std::runtime_error("Control refused the Event subscription " + spec.topic);
    return publications.size() + subscriptions.size();
}

R2KERNEL_SDK_COLD bool R2KernelClient::keep_registration(const RegistrationStep& send_status,
    const RegistrationStep& register_again, const RegistrationLog& log) {
    return detail::keep_registration(impl_->registration_keeper, send_status, register_again, log);
}

R2KERNEL_SDK_COLD bool R2KernelClient::registration_lost() const {
    return detail::registration_lost(impl_->registration_keeper.get());
}

R2KERNEL_SDK_COLD std::string R2KernelClient::reregister_subsystem() {
    return impl_->reregister_subsystem();
}

R2KERNEL_SDK_COLD std::size_t R2KernelClient::restore_event_registrations() {
    return impl_->restore_event_registrations();
}

}  // namespace r2kernel::core

#undef R2KERNEL_SDK_COLD
