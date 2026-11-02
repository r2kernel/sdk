#ifndef R2KERNEL_CORE_CONTROL_H
#define R2KERNEL_CORE_CONTROL_H

/*
 * Control channel C ABI: the request/response transport that carries
 * Procedure and Operation calls. The SDTR Control envelope codecs
 * (r2kernel_codec_{encode,decode}_control_{request,response}_v2) are
 * declared in r2kernel_core.h.
 */

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct R2KernelControlClient R2KernelControlClient;
typedef struct R2KernelControlServer R2KernelControlServer;

/*
 * Signals: a send to a closed peer fails (SystemError -3) and never raises
 * SIGPIPE, so a host does not need to ignore SIGPIPE.
 */

int r2kernel_control_client_create(const char* identity, R2KernelControlClient** out_handle);
int r2kernel_control_client_close(R2KernelControlClient* handle);
int r2kernel_control_client_connect(R2KernelControlClient* handle, const char* name, const char* host, int32_t port);
int r2kernel_control_client_connect_bound(
    R2KernelControlClient* handle,
    const char* name,
    const char* host,
    int32_t port,
    const char* local_bind_host
);
int r2kernel_control_client_send(
    R2KernelControlClient* handle,
    const char* name,
    const char* topic,
    const uint8_t* payload,
    size_t payload_len,
    int64_t ts,
    float period_ms,
    float deadline_ms
);
int r2kernel_control_client_recv(
    R2KernelControlClient* handle,
    const char* name,
    int64_t timeout_ms,
    int64_t* out_ts,
    uint16_t* out_status,
    uint8_t* out_buf,
    size_t out_cap,
    size_t* out_len
);

int r2kernel_control_server_create(R2KernelControlServer** out_handle);
int r2kernel_control_server_bind(R2KernelControlServer* handle, const char* host, int32_t port, int32_t* out_port);
int r2kernel_control_server_close(R2KernelControlServer* handle);
/*
 * Server operations bind each response to the exact accepted connection and
 * request through a request token. A token can be sent once and is
 * invalidated when the server observes its connection close. recv rejects a
 * second request on a connection before its first response is sent.
 */
int r2kernel_control_server_recv_with_token(
    R2KernelControlServer* handle,
    int64_t timeout_ms,
    uint8_t* out_sender,
    size_t out_sender_cap,
    size_t* out_sender_len,
    uint8_t* out_topic,
    size_t out_topic_cap,
    size_t* out_topic_len,
    uint8_t* out_payload,
    size_t out_payload_cap,
    size_t* out_payload_len,
    int64_t* out_ts,
    float* out_period_ms,
    float* out_deadline_ms,
    uint64_t* out_request_token
);
int r2kernel_control_server_send_with_token(
    R2KernelControlServer* handle,
    uint64_t request_token,
    int64_t ts,
    uint16_t status,
    const uint8_t* payload,
    size_t payload_len
);

#ifdef __cplusplus
}
#endif

#endif // R2KERNEL_CORE_CONTROL_H
