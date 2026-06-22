/**
 * Proactive win-back — runs ON Twilio (Twilio Functions).
 *
 * Finds loyal customers who've gone quiet and sends them an approved WhatsApp
 * template ("we miss you"), in the same chat as their stamps. Reads the same
 * Sync Maps the inbound handler writes.
 *
 * This file is *.protected.js, so Twilio requires a valid signature to call it.
 * Trigger it on a schedule with any free cron (e.g. cron-job.org) hitting its
 * URL — Twilio Functions have no built-in scheduler.
 *
 * Requires each venue in RESTAURANTS to have:
 *   whatsapp_number, name, reward, winback_template_sid,
 *   inactive_days (default 5), min_scans (default 3), nudge_cooldown_days (5)
 */

function daysBetween(iso, now) {
  if (!iso) return null;
  const then = new Date(iso);
  if (isNaN(then)) return null;
  return Math.floor((now - then) / (1000 * 60 * 60 * 24));
}

exports.handler = async function (context, event, callback) {
  const client = context.getTwilioClient();
  const svc = context.SYNC_SERVICE_SID;
  const now = new Date();
  let venues = [];
  try {
    venues = JSON.parse(context.RESTAURANTS || "[]");
  } catch (e) {
    venues = [];
  }

  const summary = [];
  for (const v of venues) {
    if (!v.winback_template_sid) {
      summary.push(`${v.id}: skipped (no winback_template_sid)`);
      continue;
    }
    const inactiveDays = parseInt(v.inactive_days, 10) || 5;
    const minScans = parseInt(v.min_scans, 10) || 3;
    const cooldown = parseInt(v.nudge_cooldown_days, 10) || 5;
    const mapName = `cards_${v.id}`;

    let items = [];
    try {
      items = await client.sync.v1
        .services(svc)
        .syncMaps(mapName)
        .syncMapItems.list({ limit: 1000 });
    } catch (e) {
      summary.push(`${v.id}: no cards yet`);
      continue;
    }

    let sent = 0;
    for (const item of items) {
      const card = item.data || {};
      if ((card.total_scans || 0) < minScans) continue;
      const gap = daysBetween(card.updated_at, now);
      if (gap === null || gap < inactiveDays) continue;
      const nudgedGap = daysBetween(card.last_nudged_at, now);
      if (nudgedGap !== null && nudgedGap < cooldown) continue;

      await client.messages.create({
        from: `whatsapp:${v.whatsapp_number}`,
        to: `whatsapp:${card.phone}`,
        contentSid: v.winback_template_sid,
        contentVariables: JSON.stringify({
          1: v.name,
          2: String(gap),
          3: v.reward,
        }),
      });

      card.last_nudged_at = now.toISOString();
      await client.sync.v1
        .services(svc)
        .syncMaps(mapName)
        .syncMapItems(item.key)
        .update({ data: card });
      sent += 1;
    }
    summary.push(`${v.id}: ${sent} nudged`);
  }

  const response = new Twilio.Response();
  response.setStatusCode(200);
  response.appendHeader("Content-Type", "application/json");
  response.setBody({ ok: true, summary });
  return callback(null, response);
};
