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


def same_origin(origin, host):
    if not origin or not host:
        return False
    return urlsplit(origin).netloc.lower() == host.lower()
