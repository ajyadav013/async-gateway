"""SFTP, and the SSH host-key verification every session is held to.

This module used to hardcode a ``known_hosts`` of ``None``, so ``asyncssh``
accepted **any** server key on **every** session and no consumer could
turn verification on: no ``known_hosts`` key was read from ``self.info``
at all. ``client_keys`` was not passed either, so the host process's
ambient ``~/.ssh/id_*`` identities, and its ssh-agent's, were offered to
whatever answered (C4). Four rules replace that, and they are what this
module owns:

* **Verification is on, and the default is asyncssh's own.**
  ``asyncssh.connect`` is called with no ``known_hosts`` argument at all.
  Omitting it is how asyncssh is asked for its documented
  ``~/.ssh/known_hosts`` resolution; passing a value -- any value,
  including the empty tuple that is its default today -- would be this
  library restating a decision it does not own, and is how the next
  reader comes to change the default by editing one literal here.
* **Pinning is explicit.** ``protocol_info['known_hosts']`` takes
  anything asyncssh accepts, and ``protocol_info['host_key']`` takes one
  trusted key and is expressed in asyncssh's own ``(host keys, CA keys,
  revoked keys)`` pinning form.
* **The bypass has to be asked for by its own name.** Only the boolean
  ``insecure_skip_host_key_check=True`` disables verification, and it
  logs a warning naming the risk on every use. Nothing else reaches it:
  not any other truthy value, so a config file's ``'false'`` cannot turn
  verification off by being a non-empty string; not ``verify_ssl``,
  which is the HTTP family's TLS switch and says nothing about SSH host
  keys; and not a caller-supplied ``known_hosts`` of ``None``, which is
  asyncssh's spelling of the same thing and is rejected rather than
  quietly honoured.
* **No ambient identity is ever offered.** ``client_keys=None`` is
  passed by default -- ``None``, and not the empty list that reads like
  "offer nothing". asyncssh's ``prepare`` tests ``client_keys`` for
  truthiness, so ``[]`` falls through to the same
  ``load_default_keypairs()`` branch an omitted argument takes and
  additionally points ``agent_path`` at ``SSH_AUTH_SOCK``: it offers the
  host process's ``~/.ssh/id_*`` files *and* every identity its
  ssh-agent holds. Only ``None`` reaches the branch that offers neither.
  A caller doing key-based authentication supplies
  ``protocol_info['client_keys']`` and the value is forwarded unchanged;
  supplied together with a password, both are offered in asyncssh's own
  order -- public key first, password as the fallback.

What is deliberately *not* fixed here. The blanket ``except Exception``,
the fabricated ``999`` status, the wall-clock duration and the
``return True`` belong to R17 and are left exactly as they are. The
host-key failures below are raised from their own ``except`` clause, and
an ``except`` clause cannot be caught by a sibling clause of the same
``try`` statement -- so a ``HostKeyError`` reaches the entry point's one
conversion point while everything else keeps its current behaviour until
R17 rewrites it.
"""

import logging
import time
from typing import Any, Dict, List, Optional, Text, Union

from async_gateway.helpers.internal.base import BaseRequestClass
from async_gateway.utils.exceptions import ConfigurationError, HostKeyError
from async_gateway.utils.redaction import redact_url, redact_value

import asyncssh

logger = logging.getLogger(__name__)

# The three `protocol_info` keys that each name a host-key policy. They
# are alternatives, not layers: naming two of them is a caller asking for
# two different policies at once, and there is no reading of that pair
# which is not a guess.
HOST_KEY_POLICY_KEYS: tuple[Text, ...] = (
    'host_key',
    'insecure_skip_host_key_check',
    'known_hosts',
)


class SFTPRequest(BaseRequestClass):
    """Implements asyncssh to make sftp calls."""

    def __init__(self, *args, **kwargs) -> None:
        """Initializing the ftp request class."""
        super(SFTPRequest, self).__init__(*args, **kwargs)

        self.port: int = self.info.get('port', 22)
        self.user: Text = self.auth.login
        self.password: Text = self.auth.password
        self.mode_: Text = self.info.get('mode', None)
        self.remote_path: Text = self.info.get('remote_path', None)
        self.local_path: Text = self.info.get('local_path', None)
        self.remote_files: Optional[Union[List, Text]] = self.remote_path
        self.additional_arguments: Dict = self.info.get('additional_arguments', {})

        # `Any` on all three deliberately: what asyncssh accepts for
        # `known_hosts` and `client_keys` is its own union of paths, key
        # objects, byte strings and callables, and restating that union
        # here would only go stale against the library. These values are
        # forwarded, never interpreted.
        self.known_hosts: Any = self.info.get('known_hosts')
        self.host_key: Any = self.info.get('host_key')
        self.client_keys: Any = self.info.get('client_keys') or None
        # `is True`, not `bool()`: a config file's string `'false'` is
        # truthy, and a bypass that a caller believes they turned off is
        # the one failure mode this keyword exists to make impossible.
        self.insecure_skip_host_key_check: bool = (
            self.info.get('insecure_skip_host_key_check') is True)

        self.connect_options: Dict[Text, Any] = self._connect_options()

    def _connect_options(self) -> Dict[Text, Any]:
        """Return the keyword arguments ``asyncssh.connect`` is called with.

        Built once, at construction, so the caller-configuration errors it
        can raise escape ``request()`` synchronously the way every other
        ``ConfigurationError`` does, rather than being caught by
        ``handle_request``'s own handling and reported to the caller as a
        failed network call.

        Returns:
            The connection keyword arguments. ``known_hosts`` is *absent*
            from them unless the caller named a policy, because its
            absence is precisely how asyncssh is asked for its own
            ``~/.ssh/known_hosts`` resolution.

        Raises:
            ConfigurationError: If ``protocol_info`` names more than one
                host-key policy, or spells the bypass as a
                ``known_hosts`` of ``None`` rather than by its own name.
        """
        if 'known_hosts' in self.info and self.known_hosts is None:
            raise ConfigurationError(
                'protocol_info["known_hosts"] = None turns SSH host key '
                'verification off entirely, and this library accepts '
                'that only under its own name: set '
                'protocol_info["insecure_skip_host_key_check"] = True if '
                'that is what you mean')

        # Read off the parsed attributes rather than off `self.info`: a
        # caller who wrote `insecure_skip_host_key_check=False` has named
        # no policy at all, and a membership test on the raw mapping
        # would read that explicit "no" as a second policy and reject a
        # call that is not ambiguous.
        declared = [
            name for name, asked in (
                ('host_key', self.host_key is not None),
                ('insecure_skip_host_key_check',
                 self.insecure_skip_host_key_check),
                ('known_hosts', self.known_hosts is not None),
            ) if asked
        ]
        if len(declared) > 1:
            raise ConfigurationError(
                f'protocol_info sets {declared}, but the host key policy '
                f'is one decision: name exactly one of '
                f'{list(HOST_KEY_POLICY_KEYS)}, or none of them to take '
                f'the asyncssh default known_hosts resolution')

        options: Dict[Text, Any] = {
            'host': self.url,
            'username': self.user,
            'password': self.password,
            'port': self.port,
            # `None` offers nothing, and only `None` does: an empty list
            # is falsy but is not `None`, so asyncssh's `prepare` reads
            # it as unset and hands the host process's own `~/.ssh/id_*`
            # identities -- and its ssh-agent's -- to whatever answered,
            # exactly as an omitted argument does.
            'client_keys': self.client_keys,
        }

        if self.insecure_skip_host_key_check:
            logger.warning(
                'SSH host key verification is disabled for this SFTP '
                'session: any host that answers can impersonate the '
                'endpoint, collect the credentials offered to it and '
                'read or alter every byte transferred. Pin the server '
                'with protocol_info["known_hosts"] or '
                'protocol_info["host_key"] instead.',
                extra={
                    'host': redact_value(
                        self.url, extra_params=self.redact_params),
                },
            )
            # The one place asyncssh's own opt-out is reachable from, and
            # it is reachable only through the keyword just warned about.
            options['known_hosts'] = None
        elif self.host_key is not None:
            # asyncssh's pinning form: trusted host keys, trusted CA
            # keys, revoked keys.
            options['known_hosts'] = ([self.host_key], [], [])
        elif self.known_hosts is not None:
            options['known_hosts'] = self.known_hosts

        return options

    def _host_key_error(self, err: asyncssh.Error) -> HostKeyError:
        """Return the typed failure for an unverifiable server host key.

        Args:
            err: The ``asyncssh`` handshake failure.
                ``HostKeyNotVerifiable`` is the presented key not matching
                what is trusted; ``KeyExchangeFailed`` is the handshake
                ending without an agreed host key algorithm, and its own
                text names the algorithms, which is what makes that case
                actionable. asyncssh reports a cipher or MAC negotiation
                failure through that same class, so classifying it here
                over-reports the cause and under-reports nothing --
                the direction this module errs in.

        Returns:
            A ``HostKeyError``: code ``HOST_KEY``, status 495, with a
            message naming the option that fixes it, so a host simply
            absent from ``~/.ssh/known_hosts`` -- a container, every time
            -- reports something the caller can act on rather than a bare
            asyncssh string.
        """
        return HostKeyError(
            f'SSH host key verification failed for '
            f'{redact_url(self.url, extra_params=self.redact_params)}: '
            f'{err}. Trust this server explicitly with '
            f'protocol_info["known_hosts"] or protocol_info["host_key"] '
            f'if it is not in ~/.ssh/known_hosts.')

    async def handle_request(self):
        """Make network call using asyncssh over sftp protocol.

        Raises:
            HostKeyError: When the server's host key does not verify
                against the configured policy, or when the handshake ends
                without an agreed host key algorithm. Raised from its own
                ``except`` clause, which is what keeps the blanket
                handler below -- R17's to remove -- from catching it.
        """
        try:
            async with asyncssh.connect(**self.connect_options) as conn:
                async with conn.start_sftp_client() as sftp:
                    lstat = {
                        i.split(':')[0]: i.split(':')[1]
                        for i in str(await sftp.lstat(self.remote_path)).split(',')
                        }
                    # getting files list for directory
                    if 'directory' in lstat['type']:
                        remote_files: List = await sftp.listdir(self.remote_path)
                        self.additional_arguments.update({'recurse': True})

                    make_sftp_request = getattr(sftp, self.mode_.lower())
                    if self.local_path:
                        await self.circuit_breaker.failsafe.run(
                            make_sftp_request,
                            self.remote_path,
                            self.local_path,
                            **self.additional_arguments)
                    else:
                        await self.circuit_breaker.failsafe.run(
                            make_sftp_request,
                            self.remote_path,
                            **self.additional_arguments)

                    self.response['mode'] = self.mode_
                    self.response['file_stats'] = lstat
                    self.response['files'] = remote_files
                    self.response['tat'] = (time.time() - self.start_time)
                return True

        except (asyncssh.HostKeyNotVerifiable,
                asyncssh.KeyExchangeFailed) as host_key_failure:
            raise self._host_key_error(host_key_failure) from host_key_failure

        except Exception as request_error:
            self.response['status_code'] = 999
            self.response['latency'] = (time.time() - self.start_time)
            self.response['text'] = request_error

            return self.response
