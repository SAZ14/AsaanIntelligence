from __future__ import annotations

from html import escape


def _esc(s: str) -> str:
    return escape(str(s))


def render_join_page(
    venue_name: str,
    venue_slug: str,
    join_result: dict | None = None,
    error: str = "",
) -> str:
    """Mobile-friendly guest join page for permanent venue QR."""
    result_block = ""
    if join_result:
        result_block = f"""
<div class="card success">
  <h2>{_esc("Welcome back!" if join_result.get("is_returning") else "You're in!")}</h2>
  <p class="lead">{_esc(join_result.get("message", ""))}</p>
  <div class="code">{_esc(join_result.get("short_code", ""))}</div>
  <p class="hint">Show this code at checkout</p>
  <dl>
    <dt>Visits</dt><dd>{join_result.get("visit_count", 0)}</dd>
    <dt>Tier</dt><dd>{_esc(join_result.get("loyalty_tier", "none"))}</dd>
    <dt>Next reward in</dt><dd>{join_result.get("visits_to_milestone", 0)} visits</dd>
  </dl>
</div>
"""
    error_block = f'<p class="error">{_esc(error)}</p>' if error else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(venue_name)} — Join Rewards</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
         background: linear-gradient(160deg, #1a1a2e 0%, #16213e 100%);
         color: #fff; min-height: 100vh; padding: 24px 16px; }}
  .wrap {{ max-width: 400px; margin: 0 auto; }}
  h1 {{ font-size: 24px; margin-bottom: 4px; }}
  .sub {{ color: #aaa; font-size: 14px; margin-bottom: 24px; }}
  .card {{ background: rgba(255,255,255,.08); border-radius: 16px; padding: 24px;
           backdrop-filter: blur(8px); margin-bottom: 16px; }}
  .card.success {{ border: 1px solid rgba(39,174,96,.5); }}
  label {{ display: block; font-size: 13px; color: #ccc; margin-bottom: 6px; margin-top: 14px; }}
  input {{ width: 100%; padding: 12px; border: none; border-radius: 8px;
          font-size: 16px; background: rgba(255,255,255,.12); color: #fff; }}
  input::placeholder {{ color: #888; }}
  button {{ width: 100%; margin-top: 20px; padding: 14px; border: none; border-radius: 10px;
           background: #27ae60; color: #fff; font-size: 16px; font-weight: 600; cursor: pointer; }}
  button.secondary {{ background: transparent; border: 1px solid rgba(255,255,255,.3); margin-top: 10px; }}
  .code {{ font-size: 48px; font-weight: 800; letter-spacing: 8px; text-align: center;
          margin: 16px 0; color: #2ecc71; }}
  .hint {{ text-align: center; color: #aaa; font-size: 13px; }}
  .lead {{ font-size: 15px; line-height: 1.5; margin-bottom: 12px; }}
  dl {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 16px; font-size: 14px; }}
  dt {{ color: #888; }}
  dd {{ font-weight: 600; }}
  .error {{ color: #e74c3c; margin-bottom: 12px; font-size: 14px; }}
  .opt {{ display: flex; align-items: center; gap: 8px; margin-top: 16px; font-size: 13px; color: #ccc; }}
  .opt input {{ width: auto; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>{_esc(venue_name)}</h1>
  <p class="sub">Join our rewards — get offers on WhatsApp when you visit</p>
  {error_block}
  {result_block}
  <div class="card">
    <form method="post" action="/join/{_esc(venue_slug)}">
      <label for="display_name">Your name</label>
      <input id="display_name" name="display_name" placeholder="Ayesha" required
             value="{_esc(join_result.get("display_name", "") if join_result else "")}">

      <label for="phone">WhatsApp number</label>
      <input id="phone" name="phone" type="tel" placeholder="+923001234567" required
             value="{_esc(join_result.get("phone", "") if join_result else "")}">

      <label class="opt">
        <input type="checkbox" name="opted_in" value="true" checked>
        Send me rewards and offers on WhatsApp
      </label>

      <button type="submit">Join rewards</button>
    </form>
    <form method="get" action="/join/{_esc(venue_slug)}">
      <input type="hidden" name="lookup" value="1">
      <label for="lookup_phone" style="margin-top:16px">Already joined? Enter your number</label>
      <input id="lookup_phone" name="phone" type="tel" placeholder="+923001234567">
      <button type="submit" class="secondary">Look up my rewards</button>
    </form>
  </div>
</div>
</body>
</html>"""
