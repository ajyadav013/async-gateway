"""Exercise every bounded GCS request shape without contacting Google Cloud.

Run ``python examples/gcs_example.py``. The script uses an in-process
deterministic strategy double, so it performs no live GCP, ADC discovery,
metadata-server, emulator, DNS, network, or local-file operation and creates
no actual signed URL. The six calls still pass through the public selector,
strict validation, target parsing, shared envelope, and dispatch boundary.

``GCS`` accepts ``gs://bucket[/object-or-prefix]`` with ``auth=None`` and the
five commands download, upload, head, list, and signed_url.
It uses a strict allowlist: an unknown key or an option from another command
is rejected before provider work.

Authentication uses Application Default Credentials (ADC), including
Workload Identity, and does not accept service-account JSON or raw key
material. Use least-privilege object permissions. Direct Signing credentials
can sign URLs. Validated impersonation additionally requires Service Account
Token Creator and ``iam.serviceAccounts.signBlob`` permission on the target
service account.

Provider and credential work runs off-loop with a finite timeout.
Operational calls use ``retry=None`` because the gateway circuit breaker owns
retries. Signed URLs bypass the gateway circuit breaker and use no gateway
retry. The private four-worker/four-lease provider pool has no queue, and its
safe telemetry excludes target and credential data. Shutdown is idempotent.
Shutdown is deferred until active leases drain to zero. Path helpers retain
their
existing default-executor behavior while the GCS lease remains held.

Uploads are create-only by default. Downloads are generation-pinned and use
sequential inclusive 64-KiB ranges with ``raw_download=True``. Transparent
decompression is explicitly disabled, so output is the exact stored/raw
representation. The gateway does not verify CRC; returned ``crc32c`` is
metadata only.

Listing fetches one page bounded by ``max_items``. ``page_token`` is an opaque,
non-empty string capped at 4096-byte when measured as UTF-8 bytes. A valid
``page_token`` is preserved exactly unchanged without normalization.

Signed URLs support V4 GET/PUT only. ``expires_in_seconds`` is in ``1..3600``,
defaults to ``900``, and becomes a relative ``timedelta``. A signed URL is a
bearer capability that the gateway cannot revoke before expiry. Signed PUT
binds the dedicated SDK ``content_type``, ``max_upload_bytes``, and generation
header. The SDK uses a two-entry SDK header mapping; the client must send the
separate three-entry header contract below. Cloud Storage enforces the signed
headers, permissions, and expiry; this no-live-GCP example does not live-test
that provider-side enforcement.

**GCS command option allowlists.**

| Operation | Required keys | Optional keys |
|---|---|---|
| ``download`` | command, local_path | max_response_bytes, \
if_generation_match, timeout, circuit_breaker_config, redact_query_params |
| ``upload`` | command, local_path | max_upload_bytes, if_generation_match, \
timeout, circuit_breaker_config, redact_query_params |
| ``head`` | command | if_generation_match, timeout, \
circuit_breaker_config, redact_query_params |
| ``list`` | command | max_items, page_token, timeout, \
circuit_breaker_config, redact_query_params |
| ``signed_url GET`` | command, method | expires_in_seconds, \
signing_service_account, timeout, redact_query_params |
| ``signed_url PUT`` | command, method, content_type, max_upload_bytes | \
expires_in_seconds, signing_service_account, if_generation_match, timeout, \
redact_query_params |

**GCS success detail schemas.**

| Operation | Exact ``protocol_details`` keys |
|---|---|
| ``download`` | command, bucket, key, local_path, bytes_written, etag, \
generation, crc32c |
| ``upload`` | command, bucket, key, local_path, bytes_read, etag, \
generation, metageneration, crc32c |
| ``head`` | command, bucket, key, content_length, content_type, etag, \
generation, metageneration, last_modified, crc32c, metadata |
| ``list`` | command, bucket, prefix, items, item_count, is_truncated, \
next_page_token |
| ``signed_url GET`` | command, method, bucket, key, expires_in_seconds, \
signed_url |
| ``signed_url PUT`` | command, method, bucket, key, expires_in_seconds, \
signed_url, content_type, max_upload_bytes, if_generation_match, \
required_headers |

**GCS list item schema.**

| Schema | Exact keys |
|---|---|
| ``list item`` | key, size, content_type, etag, generation, last_modified, \
crc32c |

**GCS signed PUT header contracts.**

| Surface | Name | Exact value |
|---|---|---|
| SDK dedicated argument | ``content_type`` | ``content_type`` |
| SDK headers | ``x-goog-content-length-range`` | \
``1,<max_upload_bytes>`` |
| SDK headers | ``x-goog-if-generation-match`` | \
``str(if_generation_match)`` |
| public ``required_headers`` | ``content-type`` | ``content_type`` |
| public ``required_headers`` | ``x-goog-content-length-range`` | \
``1,<max_upload_bytes>`` |
| public ``required_headers`` | ``x-goog-if-generation-match`` | \
``str(if_generation_match)`` |

**GCS out-of-scope capabilities.**

| Capability | Status |
|---|---|
| ``delete`` | unsupported |
| bucket administration | unsupported |
| custom endpoint | unsupported |
| signed POST | unsupported |
| signed DELETE | unsupported |
| arbitrary headers | unsupported |
| arbitrary query parameters | unsupported |
"""

import asyncio
from unittest.mock import patch

from asyncio_gateway.asyncio_gateway import request
from asyncio_gateway.logic.gcs_client import GcsRequest
from asyncio_gateway.utils.envelope import GatewayResponse, finalise_ok


async def _deterministic_success(item: GcsRequest) -> GatewayResponse:
    item.response['protocol_details'] = {'command': item.command}
    return finalise_ok(item.response, status_code=200, started=item.start_time)


async def call() -> GatewayResponse:
    """Run all six public GCS request variants through a safe double.

    Returns:
        Final deterministic gateway response envelope.
    Raises:
        asyncio.CancelledError: If the caller cancels the demonstration.
    """
    with patch.object(GcsRequest, 'handle_request', _deterministic_success):
        results = [
            await request(url='gs://demo/report.csv', auth=None,
                          protocol='GCS', protocol_info={
                              'command': 'download', 'local_path': '/tmp/d'}),
            await request(url='gs://demo/report.csv', auth=None,
                          protocol='GCS', protocol_info={
                              'command': 'upload', 'local_path': '/tmp/u'}),
            await request(url='gs://demo/report.csv', auth=None,
                          protocol='GCS', protocol_info={'command': 'head'}),
            await request(url='gs://demo/reports/', auth=None,
                          protocol='GCS', protocol_info={
                              'command': 'list', 'max_items': 10}),
            await request(url='gs://demo/report.csv', auth=None,
                          protocol='GCS', protocol_info={
                              'command': 'signed_url', 'method': 'GET'}),
            await request(url='gs://demo/report.csv', auth=None,
                          protocol='GCS', protocol_info={
                              'command': 'signed_url', 'method': 'PUT',
                              'content_type': 'text/csv',
                              'max_upload_bytes': 1024}),
        ]
    return results[-1]


if __name__ == '__main__':
    asyncio.run(call())
