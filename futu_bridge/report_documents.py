"""Verified original-report links and bounded PDF downloads.

No issuer, fiscal period or availability is inferred from a filename. Futu's
announcement page can require app access even when its attachment is public.
"""
from datetime import datetime, timezone
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urljoin, urlsplit

from .provider import ProviderError, ProviderResult
from .public_web import checked_url, public_get

NOTICE_HOSTS = {'news.futunn.com'}
PDF_HOSTS = {'newsfile.futunn.com', 'www.tencent.com', 'static.www.tencent.com', 's2.q4cdn.com', 's21.q4cdn.com', 'd18rn0p25nwr6d.cloudfront.net', 'www.sec.gov'}


def report_pdf_url(url):
    checked_url(url, PDF_HOSTS)
    parsed = urlsplit(url)
    path = parsed.path
    if not path.lower().endswith('.pdf') or parsed.fragment:
        raise ProviderError('invalid_public_url', 'Expected an original PDF document URL.', 400)
    prefixes = {'newsfile.futunn.com': ('/public/NN-PersistNoticeAttachment/',),
                'www.tencent.com': ('/wp-content/uploads/',),
                'static.www.tencent.com': ('/uploads/', '/website-2026-upload/'),
                's2.q4cdn.com': ('/',), 'd18rn0p25nwr6d.cloudfront.net': ('/',),
                's21.q4cdn.com': ('/184289198/files/doc_financials/', '/184289198/files/content_files/'),
                'www.sec.gov': ('/Archives/edgar/data/',)}
    if not path.startswith(prefixes[parsed.hostname]):
        raise ProviderError('invalid_public_url', 'Unverified original PDF route.', 400)
    if parsed.hostname == 'www.sec.gov' and not re.fullmatch(r'/Archives/edgar/data/\d{1,10}/\d{18}/[A-Za-z0-9_.-]+\.pdf', path):
        raise ProviderError('invalid_public_url', 'Expected a public SEC EDGAR PDF attachment route.', 400)
    if parsed.hostname == 's21.q4cdn.com' and (parsed.query or not re.fullmatch(
            r'/184289198/files/(?:doc_financials|content_files)/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-][A-Za-z0-9_.-]*\.pdf', path)):
        raise ProviderError('invalid_public_url', 'Expected a verified SpaceX investor-relations PDF route.', 400)
    return url


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current = None
        self.title = ''
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('href'):
            self.current = [attrs['href'], []]
        if tag == 'title':self.in_title = True

    def handle_data(self, data):
        if self.current:self.current[1].append(data)
        if self.in_title:self.title += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.current:
            self.links.append((self.current[0], ' '.join(''.join(self.current[1]).split())))
            self.current = None
        if tag == 'title':self.in_title = False


def parse_notice_documents(html, notice_id):
    parser = _Links();parser.feed(html)
    if re.search(r'\b(?:403|Access Denied|Access Forbidden)\b', parser.title, re.I):
        raise ProviderError('public_access_denied', 'Futu public announcement page requires access unavailable to the anonymous adapter.', 503)
    if 'wafToken' in html and ('sessionStorage' in html or 'sessionstorage' in html.lower()):
        raise ProviderError('desktop_context_required', 'The announcement service returned a browser challenge; use the working desktop context.', 503)
    # Only actual rendered anchor URLs are returned; no URL IDs are fabricated.
    documents = []
    seen = set()
    for href, title in parser.links:
        url = urljoin('https://news.futunn.com/notice/' + notice_id, href)
        if urlsplit(url).hostname in PDF_HOSTS and urlsplit(url).path.lower().endswith('.pdf'):
            report_pdf_url(url)
            if url not in seen:
                seen.add(url)
                documents.append({'url': url, 'title': title, 'format': 'pdf', 'verified_download': False})
    if not documents:
        raise ProviderError('document_links_unavailable', 'The public announcement response has no verified original PDF attachment links.', 503)
    return {'notice_id': notice_id, 'title': parser.title.strip(), 'documents': documents, 'count': len(documents)}


def announcement_documents(notice_id):
    value = str(notice_id)
    if isinstance(notice_id, bool) or not re.fullmatch(r'[1-9]\d{0,19}', value):
        raise ProviderError('invalid_query', 'notice_id must be a positive public announcement ID.', 400)
    url = 'https://news.futunn.com/notice/' + value
    body, doc = public_get(url, NOTICE_HOSTS)
    try:
        data = parse_notice_documents(body.decode('utf-8'), value)
    except UnicodeError:
        raise ProviderError('public_result_invalid', 'Announcement page is not valid UTF-8.', 502) from None
    return ProviderResult(data, {'provider': 'futu_public_announcements', 'kind': 'historical',
                                'authentication': 'anonymous', 'document': doc,
                                'observed_at': datetime.now(timezone.utc).isoformat()},
                          ['This resolves a specified announcement; it does not claim complete filing history.'])


def download_report_pdf(url, destination, max_bytes=50*1024*1024):
    """Download an observed public PDF atomically, refusing to overwrite files.

    This local utility writes a user-chosen path; it is intentionally not exposed
    as an HTTP endpoint that can write arbitrary server filesystem paths.
    """
    report_pdf_url(url)
    if type(max_bytes) is not int or not 1024 <= max_bytes <= 100*1024*1024:
        raise ProviderError('invalid_query', 'PDF limit must be 1 KiB through 100 MiB.', 400)
    path = Path(destination).expanduser().resolve()
    if path.suffix.lower() != '.pdf' or not path.parent.is_dir():
        raise ProviderError('invalid_query', 'Destination must be a .pdf file in an existing directory.', 400)
    if path.exists():
        raise ProviderError('destination_exists', 'Destination already exists; choose a new PDF path.', 409)
    body, document = public_get(url, PDF_HOSTS, max_bytes=max_bytes)
    report_pdf_url(document['url'])
    if not body.startswith(b'%PDF-') or b'%%EOF' not in body[-4096:]:
        raise ProviderError('invalid_pdf', 'The downloaded response is not a complete PDF document.', 502)
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix='.futu-report-', suffix='.tmp', dir=path.parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(body)
        # A same-directory hard link publishes the complete file atomically and
        # refuses to replace a concurrently created destination.
        os.link(temp_path, path)
    except FileExistsError:
        raise ProviderError('destination_exists', 'Destination already exists; choose a new PDF path.', 409) from None
    finally:
        if temp_path is not None:
            Path(temp_path).unlink(missing_ok=True)
    return ProviderResult({'path': str(path), 'format': 'pdf', 'bytes': len(body),
                           'sha256': document['sha256'], 'pdf_signature_verified': True,
                           'content_identity_verified': False},
                          {'provider': 'futu_public_attachment' if urlsplit(url).hostname == 'newsfile.futunn.com' else 'issuer_public_attachment',
                           'kind': 'historical', 'authentication': 'anonymous', 'document': document,
                           'observed_at': datetime.now(timezone.utc).isoformat()},
                          ['A valid PDF signature does not verify its issuer, fiscal period or completeness of filing history.'])


def parse_tencent_reports(html, year=None):
    parser = _Links();parser.feed(html)
    documents = {}
    for href, title in parser.links:
        match = re.fullmatch(r'(19\d\d|20\d\d) (Annual|Interim) Report', title)
        if not match or (year is not None and int(match[1]) != year):
            continue
        url = urljoin('https://www.tencent.com/investors/financial-reports/', href)
        report_pdf_url(url)
        if urlsplit(url).hostname not in ('www.tencent.com', 'static.www.tencent.com'):
            raise ProviderError('public_result_invalid', 'Unexpected issuer report host.', 502)
        documents[url] = {'url': url, 'title': title, 'year': int(match[1]),
                          'period_type': match[2].lower(), 'format': 'pdf', 'verified_download': False}
    if not documents:
        if not any(re.fullmatch(r'(19\d\d|20\d\d) (Annual|Interim) Report', title) for _, title in parser.links):
            raise ProviderError('public_result_invalid', 'Issuer report archive layout is unrecognized.', 502)
    return sorted(documents.values(), key=lambda d: (d['year'], d['period_type'] == 'annual'), reverse=True)


def issuer_reports(symbol, year=None):
    """List Tencent annual/interim PDFs from its issuer archive, explicitly separate from Futu.

    Other issuer adapters are not implemented. No symbol-only URL is guessed.
    """
    if symbol != 'HK.00700':
        raise ProviderError('issuer_not_supported', 'The verified issuer archive adapter currently supports HK.00700 only.', 422)
    if year is not None and (type(year) is not int or not 1970 <= year <= 2099):
        raise ProviderError('invalid_query', 'year must be an integer from 1970 through 2099.', 400)
    url = 'https://www.tencent.com/investors/financial-reports/'
    body, document = public_get(url, {'www.tencent.com'})
    try:
        reports = parse_tencent_reports(body.decode('utf-8'), year)
    except UnicodeError:
        raise ProviderError('public_result_invalid', 'Issuer archive is not valid UTF-8.', 502) from None
    return ProviderResult({'symbol': symbol, 'year': year, 'documents': reports, 'count': len(reports)},
                          {'provider': 'tencent_issuer_archive', 'kind': 'historical', 'authentication': 'anonymous',
                           'document': document, 'observed_at': datetime.now(timezone.utc).isoformat()},
                          ['This explicit issuer source is separate from Futu. Archive completeness and point-in-time availability are not verified.'])
