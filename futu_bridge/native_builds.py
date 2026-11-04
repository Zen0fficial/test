"""Exact desktop build identities and the few non-exported native entry points.

Addresses are RVAs in the hash-identified modules, never offsets shared across
unknown releases. Exported methods are still resolved by their decorated names.
"""

from dataclasses import dataclass

from .provider import ProviderError


@dataclass(frozen=True)
class DesktopBuild:
    version: str
    qotbase_sha256: str
    corebiz_sha256: str
    ftnews_sha256: str
    ftquote_sha256: str
    transport_reply_rva: int
    ticker_destroy_rva: int
    option_release_rva: int
    option_release_control_block: bool
    news_flag_rva: int

    def agent_configuration(self):
        return {name: getattr(self, name) for name in (
            "version", "transport_reply_rva", "ticker_destroy_rva",
            "option_release_rva", "option_release_control_block", "news_flag_rva",
        )}


SUPPORTED_BUILDS = (
    DesktopBuild(
        version="16.33.17808",
        qotbase_sha256="8b2ed71ee723d7e8ffabba8df4e8e8aaa1f09ce5ec2a3b16586f4109a2a0246e",
        corebiz_sha256="1d48dc2712b4de4034c68316f7747775660720b0be90914aaa5b06c499450beb",
        ftnews_sha256="9cdc686720c4e98bb42fcf08349f231be6179014b8cddd54c2f4df88cb964775",
        ftquote_sha256="899db0ccb14c1f4d818db87f976d4a0dd3994ba751687ed4d72e04c51e72bb33",
        transport_reply_rva=0xA4F80,
        ticker_destroy_rva=0xFB770,
        option_release_rva=0x44B50,
        option_release_control_block=False,
        news_flag_rva=0x69C80,
    ),
    DesktopBuild(
        version="16.37.27308",
        qotbase_sha256="aade8221c680594e06b7d055788c2306db8539ecf48db695137766e1c7371d52",
        corebiz_sha256="8a903c0015d4a297abe14258cac641d10a0e3f204f5ab6f7a87785d14489455e",
        ftnews_sha256="8c538cce9e71710edc931e086bd2ce8518689ab144da5d16f172ac7753af240c",
        ftquote_sha256="df888fc851b978aa7560cdf7004ebf00c129088f23a897e83c91711e04667654",
        transport_reply_rva=0xA69D0,
        ticker_destroy_rva=0xFF600,
        # This release factors _Ref_count_base::_Decref into a helper. Its
        # argument is the control block, whereas 16.33 takes the shared_ptr.
        option_release_rva=0x167A60,
        option_release_control_block=True,
        news_flag_rva=0x6A3C0,
    ),
)
SUPPORTED_VERSIONS = tuple(build.version for build in SUPPORTED_BUILDS)


def select_build(qotbase_sha256, corebiz_sha256):
    """Reject unknown modules and mixed-version quote/transport pairs."""
    for build in SUPPORTED_BUILDS:
        if build.qotbase_sha256 == qotbase_sha256 and build.corebiz_sha256 == corebiz_sha256:
            return build
    raise ProviderError(
        "version_unsupported", "Desktop modules do not match a supported build; refusing native calls.", 503,
        {"supported_versions": list(SUPPORTED_VERSIONS), "qotbase_sha256": qotbase_sha256,
         "corebiz_sha256": corebiz_sha256},
    )
