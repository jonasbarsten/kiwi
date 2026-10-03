"""Who may use the page: private networks only, and POSTs only from the page itself."""
import ipaddress
from urllib.parse import urlsplit


def is_private(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


def host_allowed(host, local_address, names):
    """The Host header must name this machine (stops DNS rebinding from other sites).

    `local_address` is the address the client connected to; `names` are this
    machine's own host names.
    """
    if not host:
        return False
    host = host.lower()
    if host.startswith('['):
        hostname = host[1:].split(']', 1)[0]
    else:
        hostname = host.rsplit(':', 1)[0] if host.count(':') == 1 else host
    if hostname in {name.lower() for name in names}:
        return True
    try:
        return ipaddress.ip_address(hostname) == ipaddress.ip_address(local_address)
    except ValueError:
        return False


def same_origin(origin, host):
    if not origin or not host:
        return False
    return urlsplit(origin).netloc.lower() == host.lower()
