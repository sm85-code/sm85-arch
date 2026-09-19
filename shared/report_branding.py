"""Organization letterhead for financial PDF/Excel exports (env-driven)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class ReportBranding:
    org_name: str
    org_legal_name: str
    address: str
    village: str
    district: str
    regency: str
    province: str
    phone: str
    email: str
    tagline: str
    signatory_left_title: str
    signatory_left_name: str
    signatory_mid_title: str
    signatory_mid_name: str
    signatory_right_title: str
    signatory_right_name: str
    primary_color: str  # hex without #

    @property
    def location_line(self) -> str:
        parts = [p for p in (self.village, self.district, self.regency, self.province) if p]
        return ", ".join(parts)

    @property
    def address_block(self) -> str:
        lines = [self.address, self.location_line]
        contact = " · ".join(x for x in (self.phone, self.email) if x)
        if contact:
            lines.append(contact)
        return "\n".join(x for x in lines if x)


@lru_cache(maxsize=1)
def get_report_branding() -> ReportBranding:
    return ReportBranding(
        org_name=os.getenv("REPORT_ORG_NAME", os.getenv("ORG_NAME", "BUMDes")),
        org_legal_name=os.getenv(
            "REPORT_ORG_LEGAL_NAME",
            os.getenv("ORG_LEGAL_NAME", "Badan Usaha Milik Desa"),
        ),
        address=os.getenv("REPORT_ORG_ADDRESS", os.getenv("ORG_ADDRESS", "")),
        village=os.getenv("REPORT_ORG_VILLAGE", os.getenv("ORG_VILLAGE", "")),
        district=os.getenv("REPORT_ORG_DISTRICT", os.getenv("ORG_DISTRICT", "")),
        regency=os.getenv("REPORT_ORG_REGENCY", os.getenv("ORG_REGENCY", "")),
        province=os.getenv("REPORT_ORG_PROVINCE", os.getenv("ORG_PROVINCE", "")),
        phone=os.getenv("REPORT_ORG_PHONE", os.getenv("ORG_PHONE", "")),
        email=os.getenv("REPORT_ORG_EMAIL", os.getenv("ORG_EMAIL", "")),
        tagline=os.getenv("REPORT_ORG_TAGLINE", "Laporan Keuangan"),
        signatory_left_title=os.getenv("REPORT_SIGN_LEFT_TITLE", "Direktur / Ketua"),
        signatory_left_name=os.getenv("REPORT_SIGN_LEFT_NAME", ""),
        signatory_mid_title=os.getenv("REPORT_SIGN_MID_TITLE", "Bendahara"),
        signatory_mid_name=os.getenv("REPORT_SIGN_MID_NAME", ""),
        signatory_right_title=os.getenv("REPORT_SIGN_RIGHT_TITLE", "Mengetahui"),
        signatory_right_name=os.getenv("REPORT_SIGN_RIGHT_NAME", ""),
        primary_color=os.getenv("REPORT_PRIMARY_COLOR", "1F4E79").lstrip("#"),
    )
