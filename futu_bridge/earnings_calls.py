"""Read public Futu earnings-call metadata and subtitles linked by a native report.

The HTML hydration is parsed as JSON, never evaluated as JavaScript. Only the
report and call fields are retained; viewer, community and comment data are not.
"""
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .provider import ProviderError, ProviderResult
from .public_web import checked_url, public_get
from .report_wire import public_report_url

HUB_HOSTS = {'earningshub.futunn.com', 'www.moomoo.com'}
FEED_HOSTS = {'q.futunn.com', 'www.moomoo.com'}
MEDIA_HOSTS = {'vod.futunn.com', 'nn-sns-subtitle.futunn.com'}
LANGUAGES = {0: 'zh-Hans', 1: 'zh-Hant', 2: 'en'}


def _identity(value, name):
    value = str(value)
    if not re.fullmatch(r'[1-9]\d{0,24}', value):
        raise ValueError('Invalid ' + name)
    return value


def parse_hub(html, report):
    marker = "'__FINANCIAL_REPORT_COMPANY_PAGE_INFO__'"
    if marker not in html:
        raise ValueError('Public report page lacks its company data')
    tail = html[html.index(marker) + len(marker):]
    match = re.search(r'value:\s*JSON\.parse\(', tail)
    if not match:
        raise ValueError('Public report page uses an unknown data layout')
    serialized, unused = json.JSONDecoder().raw_decode(tail[match.end():])
    if not isinstance(serialized, str):
        raise ValueError('Invalid public report data encoding')
    state = json.loads(serialized)
    if str(state.get('pageId')) != report['report_id']:
        raise ValueError('Public report identity differs from the native link')
    if report['url_stock_id'] not in [str(x) for x in state.get('stockIds', [])]:
        raise ValueError('Public report stock identity differs from the native link')
    meeting = state.get('performanceMeeting')
    if not meeting:
        return None
    data = meeting.get('data') or {}
    if not data.get('liveUrl'):
        return None
    feed_id = _identity(data.get('feedId'), 'feed ID')
    url = checked_url(data['liveUrl'], FEED_HOSTS)
    if not re.fullmatch(r'/(?:community/)?feed/' + feed_id + r'(?:/[^/?#]*)?', urlsplit(url).path) or urlsplit(url).query:
        raise ValueError('Unknown public call URL route')
    return {'feed_id': feed_id, 'live_id': _identity(data.get('liveId'), 'live ID'),
            'title': str(data.get('title', ''))[:2000], 'url': url,
            'start_timestamp_ms': data.get('liveTime'), 'status_code': data.get('status'),
            'meeting_type_code': meeting.get('type'),
            'ai_interpretation_advertised': data.get('isDisplayAiInterpretation') is True}


def parse_call_page(html, feed_id):
    marker = 'window.__INITIAL_STATE__ = '
    if marker not in html:
        raise ValueError('Public call page lacks its detail data')
    state, unused = json.JSONDecoder().raw_decode(html[html.index(marker) + len(marker):])
    detail = state.get('detail', {})
    if detail.get('code') != 0:
        raise ValueError('Public call page reports unavailable content')
    if str(detail.get('feedCommon', {}).get('feedId')) != feed_id:
        raise ValueError('Public call identity differs from requested feed')
    videos, subtitles, seen = [], [], set()
    for module in detail.get('moduleData', []):
        for video in module.get('_original', {}).get('videoCommonInfo', []):
            url = checked_url(video.get('videoUrl'), MEDIA_HOSTS)
            if urlsplit(url).hostname != 'vod.futunn.com':
                raise ValueError('Unknown recording host')
            info = video.get('videoLanguageInfo') or {}
            labels = (video.get('videoEpisodeInfo') or {}).get('multiLangEpisodeName', {}).get('strContext', [])
            label = next((x.get('context') for x in labels if x.get('languageId') == 2), None)
            videos.append({'url': url, 'language_code': info.get('lang'), 'label': label,
                           'original_audio': info.get('isOriginalLang') is True,
                           'url_may_expire': True})
            rows = [(x, False) for x in video.get('multiLangSubtitleUrl', [])]
            if video.get('originalLangSubtitleUrl'):
                rows.append((video['originalLangSubtitleUrl'], True))
            for row, original in rows:
                sub_url = checked_url(row.get('subtitleUrl'), MEDIA_HOSTS)
                if urlsplit(sub_url).hostname != 'nn-sns-subtitle.futunn.com' or not urlsplit(sub_url).path.endswith('.srt'):
                    raise ValueError('Unknown subtitle URL')
                if sub_url in seen:
                    existing = next(x for x in subtitles if x['url'] == sub_url)
                    if existing['language_code'] != row.get('lang'):
                        raise ValueError('Conflicting subtitle language metadata')
                    existing['original_language_track'] |= original
                    continue
                seen.add(sub_url)
                subtitles.append({'url': sub_url, 'language': LANGUAGES.get(row.get('lang'), 'unknown'),
                                  'language_code': row.get('lang'), 'original_language_track': original,
                                  'format': 'srt', 'issuer_verified': False,
                                  'machine_generated_or_translated': True})
    if len(videos) > 30 or len(subtitles) > 100:
        raise ValueError('Public call contains too many media tracks')
    paragraphs = []
    def plain(node, depth=0):
        if depth > 30 or not isinstance(node, dict):
            raise ValueError('Invalid public call text tree')
        text = node.get('text', '')
        if not isinstance(text, str):
            raise ValueError('Invalid public call text')
        return text + ''.join(plain(c, depth+1) for c in node.get('children', []))
    for module in detail.get('moduleData', []):
        if module.get('type') == 0:
            text = '\n'.join(plain(node) for node in module.get('data', [])).strip()
            if text:
                paragraphs.append(text)
    if sum(map(len, paragraphs)) > 1000000 or len(paragraphs) > 10000:
        raise ValueError('Public call text exceeds limits')
    markers = ('完整記錄稿（AI生成）', '完整记录稿（AI生成）', 'Full Transcript (AI-Generated)',
               'Full Transcript (AI Generated)', 'Full transcript (AI-generated)')
    split = next((i for i,p in enumerate(paragraphs) if p in markers), None)
    page_transcript = None
    if split is not None:
        page_transcript = {'format': 'public_page_text', 'language': LANGUAGES.get(detail.get('feedCommon', {}).get('currentLang'), 'unknown'),
                           'paragraphs': paragraphs[split+1:], 'paragraph_count': len(paragraphs)-split-1,
                           'source_heading': paragraphs[split], 'machine_generated_or_translated': True,
                           'issuer_verified': False, 'timestamped': False, 'completeness_verified': False}
    return {'title': str(detail.get('feedTitle', ''))[:2000], 'feed_id': feed_id,
            'published_timestamp': detail.get('feedCommon', {}).get('timestamp'),
            'recordings': videos, 'subtitles': subtitles,
            'transcript_available': bool(subtitles), 'issuer_verified': False,
            'ai_content_unverified': True, 'page_transcript_available': page_transcript is not None,
            '_page_transcript': page_transcript}


def parse_srt(text):
    text = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text:
        raise ValueError('Empty subtitle document')
    cues = []
    stamp = r'(\d{2,3}):(\d{2}):(\d{2})[,\.](\d{3})'
    timing = re.compile(r'^' + stamp + r'\s+-->\s+' + stamp + r'(?:\s+.*)?$')
    for block in re.split(r'\n[ \t]*\n', text):
        lines = block.splitlines()
        if lines and re.fullmatch(r'\d+', lines[0].strip()):
            lines = lines[1:]
        if len(lines) < 2:
            raise ValueError('Malformed subtitle cue')
        match = timing.fullmatch(lines[0].strip())
        if not match:
            raise ValueError('Malformed subtitle timing')
        values = [int(x) for x in match.groups()]
        if any(values[i] > 59 for i in (1, 2, 5, 6)):
            raise ValueError('Invalid subtitle clock')
        start = ((values[0]*60+values[1])*60+values[2])*1000+values[3]
        end = ((values[4]*60+values[5])*60+values[6])*1000+values[7]
        if end < start or (cues and start < cues[-1]['start_ms']):
            raise ValueError('Subtitle times are out of order')
        cues.append({'start_ms': start, 'end_ms': end, 'text': '\n'.join(lines[1:])})
        if len(cues) > 20000:
            raise ValueError('Too many subtitle cues')
    return cues


def earnings_call(report_result, include_transcript=False, language='page'):
    """Return anonymous Futu call data for a verified earnings_report_link result."""
    if type(include_transcript) is not bool or language not in ('page', 'original', 'en', 'zh-Hans', 'zh-Hant'):
        raise ProviderError('invalid_query', 'Invalid transcript selection.', 400)
    source = {'provider': 'futu_public_earnings', 'kind': 'historical',
              'observed_at': datetime.now(timezone.utc).isoformat(), 'authentication': 'anonymous',
              'native_report_source': report_result.source, 'complete_history': False}
    data = {k: report_result.data.get(k) for k in ('symbol', 'stock_id', 'fiscal_period')}
    data.update({'available': False, 'meeting': None, 'call': None, 'transcript': None})
    warnings = list(report_result.warnings) + [
        'Call subtitles and interpretations may be machine generated or translated; they are not issuer-verified transcripts.',
        'A report page, recording and subtitle may contain inconsistent dates; retain source dates and verify against the issuer.',
        'Publication timestamps are not point-in-time availability guarantees. Recording links may expire.']
    report = report_result.data.get('report')
    if not report or not report.get('url'):
        return ProviderResult(data, source, warnings)
    try:
        verified = public_report_url(report['url'])
        body, source['hub_document'] = public_get(verified['url'], HUB_HOSTS)
        meeting = parse_hub(body.decode('utf-8'), verified)
        data['meeting'] = meeting
        if meeting is None:
            return ProviderResult(data, source, warnings)
        body, source['call_document'] = public_get(meeting['url'], FEED_HOSTS)
        call = parse_call_page(body.decode('utf-8'), meeting['feed_id'])
        page_transcript = call.pop('_page_transcript')
        data.update({'call': call, 'available': True})
        if include_transcript:
            if language == 'page':
                data['transcript'] = page_transcript
                if page_transcript is None:
                    warnings.append('No recognized AI transcript section is present in this public call page.')
                return ProviderResult(data, source, warnings)
            candidates = [s for s in call['subtitles'] if
                          (s['original_language_track'] if language == 'original'
                           else s['language'] == language)]
            if not candidates:
                warnings.append('The requested subtitle language is not provided by this public call page.')
            else:
                track = candidates[0]
                try:
                    body, source['transcript_document'] = public_get(track['url'], MEDIA_HOSTS, max_bytes=3*1024*1024)
                except ProviderError as error:
                    data['transcript_error'] = {'code': error.code, 'details': error.details}
                    warnings.append('The selected subtitle download is unavailable; no substitute transcript was returned.')
                    return ProviderResult(data, source, warnings)
                cues = parse_srt(body.decode('utf-8-sig'))
                data['transcript'] = dict(track, cues=cues, cue_count=len(cues),
                                          duration_ms=max(c['end_ms'] for c in cues))
        return ProviderResult(data, source, warnings)
    except (ValueError, UnicodeError, TypeError, KeyError, AttributeError) as error:
        raise ProviderError('public_result_invalid', 'Public earnings-call response failed validation.', 502,
                            {'error_type': type(error).__name__}) from None
