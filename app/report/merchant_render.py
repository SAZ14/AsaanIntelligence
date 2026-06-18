from __future__ import annotations

from dataclasses import asdict
from html import escape

from app.agents.merchant_customer import MerchantCustomerDashboard


def _esc(s: str) -> str:
    return escape(str(s))


def _pkr(v: float) -> str:
    return f"PKR {v:,.0f}"


def generate_merchant_dashboard(dashboard: MerchantCustomerDashboard) -> str:
    h = dashboard.headlines
    urg_cls = {"critical": "tag-red", "high": "tag-amber", "medium": "tag-blue"}

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Merchant Customer Agent — {_esc(h.venue_name)}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         color: #1a1a2e; background: #f4f5f7; line-height: 1.5; padding: 24px; max-width: 960px; margin: 0 auto; }}
  h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 4px; }}
  .subtitle {{ font-size: 13px; color: #666; margin-bottom: 24px; font-style: italic; }}
  .cards {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 24px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .card-label {{ font-size: 11px; font-weight: 600; text-transform: uppercase; color: #888; }}
  .card-value {{ font-size: 24px; font-weight: 800; margin-top: 4px; }}
  .card.warn {{ border-left: 4px solid #e74c3c; }}
  .card.ok {{ border-left: 4px solid #27ae60; }}
  .card.info {{ border-left: 4px solid #3498db; }}
  section {{ background: #fff; border-radius: 10px; padding: 20px; margin-bottom: 16px;
             box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  section h2 {{ font-size: 14px; font-weight: 700; text-transform: uppercase; color: #555;
               border-bottom: 2px solid #eee; padding-bottom: 6px; margin-bottom: 12px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; font-size: 11px; text-transform: uppercase; color: #888;
       padding: 6px 8px; border-bottom: 2px solid #eee; }}
  td {{ padding: 8px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }}
  .tag {{ display: inline-block; font-size: 10px; font-weight: 600; padding: 2px 8px; border-radius: 4px; }}
  .tag-red {{ background: #fde8e8; color: #c0392b; }}
  .tag-amber {{ background: #fef3e2; color: #b7791f; }}
  .tag-blue {{ background: #e8f0fe; color: #2c5282; }}
  .tag-green {{ background: #e8f8ef; color: #1e8449; }}
  .tag-gray {{ background: #eee; color: #666; }}
  .msg {{ font-size: 12px; color: #444; max-width: 320px; }}
  .footer {{ text-align: center; font-size: 11px; color: #aaa; margin-top: 20px; }}
</style>
</head>
<body>
<h1>Merchant Customer Agent</h1>
<div class="subtitle">{_esc(h.venue_name)} &middot; &ldquo;{_esc(h.tagline)}&rdquo;</div>

<div class="cards">
  <div class="card warn">
    <div class="card-label">Win-back at risk</div>
    <div class="card-value">{_esc(_pkr(h.winback_at_risk))}</div>
  </div>
  <div class="card warn">
    <div class="card-label">Lapsed / at-risk</div>
    <div class="card-value">{h.lapsed_count} / {h.at_risk_count}</div>
  </div>
  <div class="card ok">
    <div class="card-label">Ready to send</div>
    <div class="card-value">{h.ready_to_send}</div>
  </div>
  <div class="card info">
    <div class="card-label">Pending approval</div>
    <div class="card-value">{h.pending_approval}</div>
  </div>
  <div class="card info">
    <div class="card-label">Needs QR link</div>
    <div class="card-value">{h.needs_qr_link}</div>
  </div>
  <div class="card info">
    <div class="card-label">QR-linked guests</div>
    <div class="card-value">{h.qr_linked}</div>
  </div>
</div>
"""

    if dashboard.inbox:
        html += """<section>
<h2>Lapse Inbox</h2>
<table>
<tr><th>Guest</th><th>Segment</th><th>Urgency</th><th>Days away</th><th>Monthly value</th><th>Alert</th></tr>
"""
        for a in dashboard.inbox[:12]:
            cls = urg_cls.get(a.urgency, "tag-amber")
            html += f"""<tr>
<td>{_esc(a.display_name)}</td><td>{_esc(a.segment)}</td>
<td><span class="tag {cls}">{_esc(a.urgency.title())}</span></td>
<td>{a.days_since_last}d</td><td>{_esc(_pkr(a.monthly_value))}</td>
<td class="msg">{_esc(a.message)}</td></tr>
"""
        html += "</table></section>\n"

    sendable = [p for p in dashboard.pending_comms if p.sendable][:10]
    blocked = [p for p in dashboard.pending_comms if not p.sendable][:5]

    if sendable:
        html += """<section>
<h2>Pending Communications — ready to approve</h2>
<table>
<tr><th>Guest</th><th>Offer</th><th>Channel</th><th>Message preview</th></tr>
"""
        for p in sendable:
            html += f"""<tr>
<td>{_esc(p.display_name)}<br><span class="tag tag-green">{_esc(p.incentive_type)}</span></td>
<td>{_esc(p.reward_text)}</td><td>{_esc(p.channel)}</td>
<td class="msg">{_esc(p.message[:160])}{"…" if len(p.message) > 160 else ""}</td></tr>
"""
        html += "</table></section>\n"

    if blocked:
        html += """<section>
<h2>Needs action before send</h2>
<table>
<tr><th>Guest</th><th>Block reason</th><th>Offer</th></tr>
"""
        for p in blocked:
            html += f"""<tr>
<td>{_esc(p.display_name)}</td>
<td><span class="tag tag-gray">{_esc(p.block_reason)}</span></td>
<td>{_esc(p.reward_text)}</td></tr>
"""
        html += "</table></section>\n"

    html += f"""<section>
<h2>Loyalty rules (merchant config)</h2>
<table>
<tr><th>Rule</th><th>Value</th></tr>
<tr><td>Milestone every N visits</td><td>{dashboard.rules.milestone_visit_interval} visits → {dashboard.rules.milestone_discount_pct:.0f}% off</td></tr>
<tr><td>Lapsed win-back</td><td>{dashboard.rules.winback_lapsed_discount_pct:.0f}% off</td></tr>
<tr><td>Lapsing nudge</td><td>{dashboard.rules.winback_lapsing_discount_pct:.0f}% off</td></tr>
<tr><td>Corporate thanks</td><td>{dashboard.rules.corporate_discount_pct:.0f}% off</td></tr>
<tr><td>Visit streak</td><td>{dashboard.rules.streak_min_visits}+ visits in {dashboard.rules.streak_window_days}d → {_esc(dashboard.rules.streak_reward)}</td></tr>
</table>
</section>

<div class="footer">Approve messages via POST /merchant/incentives/approve &middot; Asaan Customer Agent</div>
</body>
</html>"""
    return html
