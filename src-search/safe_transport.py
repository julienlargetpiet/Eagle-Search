"""Connection-time public-IP validation for a urllib web crawler.

Requires Python 3.9+. This deliberately disables environment HTTP proxies.
Every new TCP connection is made to a previously validated IP address.
"""

import http.client
import ipaddress
import socket
import ssl
import time
import threading

from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    build_opener,
)

class RateLimiter:

    def __init__(self, delay):

        if delay < 0:
            raise ValueError("Delay must be nonnegative")

        self.delay = delay
        self.next_allowed = {}
        self.lock = threading.Lock()

    def wait(self, url):

        host = urlsplit(url).hostname

        if not host:
            raise URLError("Missing hostname")

        with self.lock:

            current = time.monotonic()

            scheduled = max(
                current,
                self.next_allowed.get(host, current)
            )

            self.next_allowed[host] = scheduled + self.delay

        remaining = scheduled - time.monotonic()

        if remaining > 0:
            time.sleep(remaining)


def connect_public(address, 
                   timeout=socket._GLOBAL_DEFAULT_TIMEOUT, 
                   source_address=None):

    """Resolve, reject non-global answers, then connect using a validated sockaddr."""
    host, port = address

    if not host or host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise OSError("Local hostnames are forbidden")

    if "%" in host:  # Reject IPv6 scope identifiers, including encoded variants.
        raise OSError("Scoped IP addresses are forbidden")

    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos:
        raise OSError("DNS returned no addresses")

    for family, socktype, proto, canonname, sockaddr in infos:

        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError as exc:
            raise OSError("Resolver returned an invalid address") from exc

        if not ip.is_global:
            raise OSError(f"Refusing non-public destination: {ip}")

    last_error = None

    for family, socktype, proto, canonname, sockaddr in infos:

        sock = socket.socket(family, socktype, proto)

        try:

            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)

            if source_address is not None:
                sock.bind(source_address)

            # IMPORTANT: sockaddr is the validated result; no second DNS lookup.
            sock.connect(sockaddr)
            return sock

        except OSError as exc:
            last_error = exc
            sock.close()

    raise last_error or OSError("Could not connect to any resolved address")


class _PublicConnectionMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # http.client.HTTPConnection.__init__ sets an instance-level
        # _create_connection attribute, so override that attribute here.

        self._create_connection = self._connect_validated

    def _connect_validated(self, address, timeout, source_address):

        if self._tunnel_host:
            raise OSError("HTTP tunnels are disabled")

        return connect_public(address, timeout, source_address)


class PublicHTTPConnection(_PublicConnectionMixin, http.client.HTTPConnection):
    pass


class PublicHTTPSConnection(_PublicConnectionMixin, http.client.HTTPSConnection):
    pass


class SafeHTTPHandler(HTTPHandler):

    def __init__(self, limiter):
        super().__init__()
        self.limiter = limiter

    def http_open(self, req):

        self.limiter.wait(req.full_url)

        return self.do_open(PublicHTTPConnection, req)

class SafeHTTPSHandler(HTTPSHandler):

    def __init__(self, limiter):
        super().__init__(context=ssl.create_default_context())
        self.limiter = limiter

    def https_open(self, req):

        self.limiter.wait(req.full_url)

        return self.do_open(
            PublicHTTPSConnection,
            req,
            context=self._context
        )


class SafeRedirects(HTTPRedirectHandler):

    def __init__(self, robots_checker):
        super().__init__()
        self.robots_checker = robots_checker

    def redirect_request(self, 
                         request, 
                         fp, 
                         code, 
                         msg, 
                         headers, 
                         newurl
    ):

        parsed = urlsplit(newurl)

        if (
            parsed.scheme.lower() not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise URLError("Invalid redirect URL")

        is_robots_request = getattr(
            request,
            "_is_robot_request",
            False
        )

        # Only check permissions for actual webpage redirects.
        if not is_robots_request:

            if not self.robots_checker(newurl):
                raise URLError(
                    f"Redirect disallowed by robots.txt: {newurl}"
                )

        redirected = super().redirect_request(
            request, fp, code, msg, headers, newurl
        )

        # Preserve the marker across multiple redirects.
        if redirected is not None and is_robots_request:
            redirected._is_robot_request = True

        return redirected

def create_safe_opener(delay, robots_checker):

    limiter = RateLimiter(delay)

    return build_opener(
        ProxyHandler({}),
        SafeRedirects(robots_checker),
        SafeHTTPHandler(limiter),
        SafeHTTPSHandler(limiter)
    )


