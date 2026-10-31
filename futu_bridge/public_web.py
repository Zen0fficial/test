"""Small, bounded, anonymous HTTPS transport for verified public document hosts."""
import hashlib
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .provider import ProviderError


def checked_url(url, hosts):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise ProviderError('invalid_public_url', 'Invalid public document URL.', 400)
    try:
        p = urlsplit(url)
        valid = p.scheme == 'https' and p.hostname in hosts and not p.username and not p.password and p.port in (None, 443)
    except ValueError:
        valid = False
    if not valid:
        raise ProviderError('invalid_public_url', 'URL is outside the verified public document hosts.', 400)
    return url


class _PublicRedirect(HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_url(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def public_get(url, hosts, max_bytes=6 * 1024 * 1024, timeout=20):
    """No session cookies, credentials, retries, or browser state are used."""
    checked_url(url, hosts)
    request = Request(url, headers={'User-Agent': 'FutuBridgeResearch/0.2 (public investor report metadata)',
                                   'Accept-Encoding': 'identity'})
    try:
        with build_opener(_PublicRedirect(hosts)).open(request, timeout=timeout) as response:
            checked_url(response.url, hosts)
            length = response.headers.get('Content-Length')
            if length is not None:
                length = length.strip()
                if not length or not length.isascii() or not length.isdigit():
                    raise ProviderError('public_response_invalid', 'Public document has an invalid content length.', 502)
                length = int(length)
            if length is not None and length > max_bytes:
                raise ProviderError('public_response_too_large', 'Public document exceeds the download limit.', 502)
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ProviderError('public_response_too_large', 'Public document exceeds the download limit.', 502)
            if length is not None and len(body) != length:
                raise ProviderError('public_response_invalid', 'Public document ended before its declared length.', 502)
            return body, {'url': response.url, 'bytes': len(body),
                          'sha256': hashlib.sha256(body).hexdigest(),
                          'content_type': response.headers.get('Content-Type', ''),
                          'http_status': response.status}
    except HTTPError as error:
        raise ProviderError('public_http_error', 'Public document service returned HTTP %d.' % error.code,
                            503 if error.code in (401, 403, 429) else 502,
                            {'http_status': error.code}) from None
    except IncompleteRead:
        raise ProviderError('public_response_invalid', 'Public document response was truncated.', 502) from None
    except (URLError, TimeoutError, OSError, ValueError) as error:
        raise ProviderError('public_request_failed', 'Public document request failed.', 502,
                            {'error_type': type(error).__name__}) from None
