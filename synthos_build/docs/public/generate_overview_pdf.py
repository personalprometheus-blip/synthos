"""
Generate the public-facing Synthos system overview PDF.

Intentionally omits:
  * Port numbers
  * LAN / WAN IP addresses
  * Hostnames tied to physical hardware
  * Internal subdomain names (admin.*, ssh.*, ssh2.*)
  * Any other operational specifics that would assist an attacker

Output: synthos_system_overview.pdf (same directory).

Run:
  python3 generate_overview_pdf.py
"""

from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, Preformatted, KeepTogether,
)


OUT = Path(__file__).parent / "synthos_system_overview.pdf"


def build():
    doc = SimpleDocTemplate(
        str(OUT),
        pagesize=LETTER,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        topMargin=0.9 * inch, bottomMargin=0.8 * inch,
        title="Synthos — System Overview",
        author="Synthos",
    )

    ss = getSampleStyleSheet()
    h1 = ParagraphStyle('h1', parent=ss['Heading1'], fontSize=20,
                        spaceAfter=10, textColor=colors.HexColor('#1a1a1a'))
    h2 = ParagraphStyle('h2', parent=ss['Heading2'], fontSize=14,
                        spaceBefore=14, spaceAfter=6,
                        textColor=colors.HexColor('#1a1a1a'))
    h3 = ParagraphStyle('h3', parent=ss['Heading3'], fontSize=11,
                        spaceBefore=10, spaceAfter=4,
                        textColor=colors.HexColor('#333333'))
    body = ParagraphStyle('body', parent=ss['BodyText'], fontSize=10,
                          leading=14, spaceAfter=6)
    small = ParagraphStyle('small', parent=ss['BodyText'], fontSize=8.5,
                           leading=11, textColor=colors.HexColor('#555555'))
    mono = ParagraphStyle('mono', parent=ss['Code'], fontSize=8.5,
                          leading=11, leftIndent=0, rightIndent=0,
                          textColor=colors.HexColor('#1a1a1a'))

    story = []

    # ── COVER ─────────────────────────────────────────────────────────────
    story += [
        Paragraph("Synthos", h1),
        Paragraph("System Overview — Hardware, Network, and Edge", h2),
        Paragraph(
            f"Version 1.0  ·  {date.today().isoformat()}  ·  "
            "Public distribution — no operational specifics",
            small,
        ),
        Spacer(1, 0.25 * inch),
        Paragraph(
            "Synthos is a supervised, paper-trading research platform built on a "
            "small fleet of single-board computers. It monitors U.S. congressional "
            "trading disclosures and market news, scores signals through a "
            "deterministic multi-agent pipeline, and executes paper trades through "
            "a regulated broker API. All decision paths are rule-based and auditable; "
            "no AI inference sits in any execution path.",
            body,
        ),
        Paragraph(
            "This document describes how the components are arranged and how they "
            "reach the public internet. It is intended for external readers — "
            "investors, partners, and auditors — and deliberately omits internal "
            "network addressing, port assignments, and hostnames.",
            body,
        ),
    ]

    # ── ARCHITECTURE PRINCIPLES ───────────────────────────────────────────
    story += [
        Paragraph("Architecture Principles", h2),
        Paragraph(
            "<b>Zero inbound attack surface.</b> No Synthos host exposes an open port "
            "to the public internet. Every inbound session originates as an outbound "
            "connection initiated by the host itself through an authenticated edge tunnel.",
            body,
        ),
        Paragraph(
            "<b>Defence in depth at the edge.</b> All public traffic passes through a "
            "managed edge provider (Cloudflare) enforcing TLS, HSTS, bot mitigation, "
            "and an identity-gated access layer for administrative routes.",
            body,
        ),
        Paragraph(
            "<b>Separation of concerns across hardware.</b> Retail-facing services, "
            "operational / back-office services, and health monitoring run on "
            "physically distinct devices. A compromise of one tier does not imply "
            "compromise of the others.",
            body,
        ),
        Paragraph(
            "<b>Deterministic, auditable decision logic.</b> Trading and news "
            "classification run through explicit rule-based gate stacks. Every "
            "decision writes a structured audit row; no decision depends on a "
            "language-model response.",
            body,
        ),
    ]

    # ── HARDWARE TOPOLOGY ─────────────────────────────────────────────────
    story += [
        Paragraph("Hardware Topology", h2),
        Paragraph(
            "The production fleet is three purpose-built nodes, each on independent "
            "power and storage, communicating only over a private local network.",
            body,
        ),
    ]

    hw = [
        ["Node", "Hardware", "Role"],
        ["Retail node",
         "Raspberry Pi 5 — NVMe SSD boot",
         "Customer-facing portal; trading, screening, "
         "news, sentiment, and validator agents; per-customer databases."],
        ["Company node",
         "Raspberry Pi 4B — SSD boot",
         "Operational back-office: audit, archive, backup, "
         "alert dispatch, internal admin API."],
        ["Monitor node",
         "Raspberry Pi Zero 2W",
         "Heartbeat receiver for all other nodes; "
         "isolated on the local network, no public exposure."],
    ]
    t = Table(hw, colWidths=[1.1 * inch, 1.8 * inch, 3.6 * inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eeeeee')),
        ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 10),
        ('FONT', (0, 1), (-1, -1), 'Helvetica', 9.5),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#bbbbbb')),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    story += [Spacer(1, 0.05 * inch), t]

    story += [
        Paragraph(
            "Each node runs a hardened Debian Linux image. Persistent data is held on "
            "local storage (NVMe on the retail node, SSD on the company node) and "
            "mirrored to encrypted off-device backup archives on a scheduled cadence. "
            "Customer personally identifiable data is encrypted at rest with a "
            "per-install symmetric key that never leaves the host.",
            body,
        ),
    ]

    story += [PageBreak()]

    # ── NETWORK + EDGE ────────────────────────────────────────────────────
    story += [
        Paragraph("Public Edge and Tunnel Model", h2),
        Paragraph(
            "The retail and company nodes each run an outbound-only tunnel client "
            "that registers with Cloudflare's edge network. Customer browsers reach "
            "the portal by way of Cloudflare, which proxies the request down the "
            "tunnel to the retail node. No public DNS record resolves to a Synthos "
            "IP address; no inbound port is open on any node's firewall.",
            body,
        ),
        Paragraph(
            "Administrative SSH to either the retail or the company node is further "
            "gated by Cloudflare Access, which requires a one-time passcode delivered "
            "to the operator's identity provider before the tunnel will bridge the "
            "session. The monitor node is reachable only from inside the private LAN.",
            body,
        ),
    ]

    story += [Paragraph("Edge Controls in Effect", h3)]
    edge = [
        ["Control", "Status"],
        ["TLS mode — Full (Strict)", "Active"],
        ["Always Use HTTPS", "Active"],
        ["Minimum TLS version 1.2", "Active"],
        ["HSTS (with includeSubDomains, nosniff)", "Active"],
        ["Browser Integrity Check", "Active"],
        ["Hotlink Protection", "Active"],
        ["Bot Fight Mode", "Active (enabled April 2026)"],
        ["Security Level — Medium", "Active (enabled April 2026)"],
        ["Cloudflare Access on admin SSH routes (OTP)", "Active"],
        ["IP allowlist for administrative routes", "Deferred — revisit post-onboarding"],
    ]
    t2 = Table(edge, colWidths=[3.8 * inch, 2.7 * inch])
    t2.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eeeeee')),
        ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 10),
        ('FONT', (0, 1), (-1, -1), 'Helvetica', 9.5),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#bbbbbb')),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    story += [Spacer(1, 0.05 * inch), t2]

    # ── FLOW DIAGRAM ──────────────────────────────────────────────────────
    story += [
        Paragraph("Public Traffic Flow", h2),
    ]

    diagram = (
        "   Customer browser\n"
        "         |\n"
        "         | HTTPS (TLS 1.2+)\n"
        "         v\n"
        "   +------------------------------+\n"
        "   |   Cloudflare edge            |\n"
        "   |   - TLS termination          |\n"
        "   |   - HSTS / bot mitigation    |\n"
        "   |   - Access OTP (admin SSH)   |\n"
        "   +--------------+---------------+\n"
        "                  |\n"
        "        Outbound-initiated tunnel\n"
        "        (no open inbound ports)\n"
        "                  |\n"
        "                  v\n"
        "   +------------------------------+\n"
        "   |   Retail node                |\n"
        "   |   - Customer portal          |\n"
        "   |   - Trading & signal agents  |\n"
        "   |   - Per-customer databases   |\n"
        "   +--------------+---------------+\n"
        "                  |\n"
        "         Private LAN (no edge)\n"
        "                  |\n"
        "                  v\n"
        "   +------------------------------+\n"
        "   |   Company node               |\n"
        "   |   - Internal admin API       |\n"
        "   |   - Audit / backup / alerts  |\n"
        "   +--------------+---------------+\n"
        "                  |\n"
        "         Private LAN heartbeats\n"
        "                  |\n"
        "                  v\n"
        "   +------------------------------+\n"
        "   |   Monitor node               |\n"
        "   |   - Heartbeat receiver       |\n"
        "   |   - LAN only, no internet    |\n"
        "   +------------------------------+\n"
    )
    story += [
        KeepTogether([
            Preformatted(diagram, mono),
            Spacer(1, 0.05 * inch),
            Paragraph(
                "Arrows show the direction a session is initiated. All internal "
                "hops are on the private local network; no node-to-node traffic "
                "traverses the public internet.",
                small,
            ),
        ]),
    ]

    # ── OUTBOUND DEPENDENCIES ─────────────────────────────────────────────
    story += [
        Paragraph("External Service Dependencies (Outbound Only)", h2),
        Paragraph(
            "The nodes reach the following third-party services over standard "
            "outbound HTTPS. These connections do not flow through the Cloudflare "
            "edge; they are direct to the provider.",
            body,
        ),
    ]
    deps = [
        ["Service", "Purpose"],
        ["Regulated broker API (Alpaca)",
         "Paper-trading order routing, equity price bars, market-news feed"],
        ["Public market data API (Yahoo Finance)",
         "Fallback price-history source when primary is throttled or unavailable"],
        ["Transactional email (Resend)",
         "Operator alerts, protective-exit confirmations, receipts"],
        ["Source control (GitHub)",
         "Scheduled code pulls during the Friday deploy window"],
    ]
    t3 = Table(deps, colWidths=[2.6 * inch, 3.9 * inch])
    t3.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eeeeee')),
        ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 10),
        ('FONT', (0, 1), (-1, -1), 'Helvetica', 9.5),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#bbbbbb')),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    story += [Spacer(1, 0.05 * inch), t3]

    # ── WHAT THIS DOC DOES NOT INCLUDE ────────────────────────────────────
    story += [
        Paragraph("Omitted from This Document", h2),
        Paragraph(
            "For security reasons, this public overview does not disclose: LAN "
            "addresses, service port numbers, hostnames, internal subdomain names, "
            "operator identity provider details, per-host firewall rules, backup "
            "archive locations, or the specific secret-management layout. Those "
            "specifics are held in internal runbooks made available under NDA on "
            "request.",
            body,
        ),
        Spacer(1, 0.2 * inch),
        Paragraph(
            "— End of document —",
            small,
        ),
    ]

    doc.build(story)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    build()
