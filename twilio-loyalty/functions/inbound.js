/**
 * Inbound WhatsApp loyalty handler — runs ON Twilio (Twilio Functions).
 *
 * Only a real QR SCAN gets a reply. The QR's pre-filled message contains the
 * marker word ("STAMP"); when the customer scans and sends it, they get a
 * stamp. Anything the customer TYPES themselves ("hi") has no marker, so it
 * gets NO answer — total silence. No server: Twilio hosts this, Twilio Sync
 * stores the cards.
 */

const SCAN_MARKER = "STAMP"; // the QR sends this; typed chit-chat won't contain it

function nowIso() { return new Date().toISOString(); }
function bar(s, r) { return "[" + "▰".repeat(Math.max(0, s)) + "▱".repeat(Math.max(0, r - s)) + `] ${s}/${r}`; }
function normalize(v) {
  return { id: v.id, name: v.name || "Rewards", stamps_required: parseInt(v.stamps_required, 10) || 5, reward: v.reward || "a free treat" };
}
function resolveVenue(to, context) {
  const digits = (to || "").replace(/\D/g, "");
  let list = [];
  try { list = JSON.parse(context.RESTAURANTS || "[]"); } catch (e) { list = []; }
  if (list.length) {
    const f = list.find((v) => (v.whatsapp_number || "").replace(/\D/g, "") === digits);
    if (f) return normalize(f);
    if (list.length === 1) return normalize(list[0]);
    return null;
  }
  return normalize({ id: context.VENUE_ID || "default", name: context.VENUE_NAME || "Sugar Rush", stamps_required: context.STAMPS_REQUIRED, reward: context.REWARD });
}
async function getCard(c, s, m, p) { try { const i = await c.sync.v1.services(s).syncMaps(m).syncMapItems(p).fetch(); return i.data; } catch (e) { return null; } }
async function ensureMap(c, s, m) { try { await c.sync.v1.services(s).syncMaps(m).fetch(); } catch (e) { await c.sync.v1.services(s).syncMaps.create({ uniqueName: m }); } }
async function putCard(c, s, m, p, d) { try { await c.sync.v1.services(s).syncMaps(m).syncMapItems(p).update({ data: d }); } catch (e) { await c.sync.v1.services(s).syncMaps(m).syncMapItems.create({ key: p, data: d }); } }
function welcomeMsg(v, c) { return `🎉 Welcome to ${v.name} Rewards!\nYou earned your 1st stamp.\n${bar(c.stamps, v.stamps_required)}\nCollect ${v.stamps_required} stamps for ${v.reward}!`; }
function stampMsg(v, c) { const r = v.stamps_required - c.stamps; const n = r === 1 ? "Just 1 more to go!" : `${r} more for ${v.reward}.`; return `⭐ Stamp added at ${v.name}!\n${bar(c.stamps, v.stamps_required)}\n${n}`; }
function completeMsg(v) { return `🏆 Card complete! You've unlocked ${v.reward.toUpperCase()} at ${v.name}.\nShow this to our staff. 🎁\n${bar(0, v.stamps_required)}`; }

exports.handler = async function (context, event, callback) {
  const twiml = new Twilio.twiml.MessagingResponse();
  const body = (event.Body || "").toUpperCase();
  // Only a real scan (carries the marker) gets a reply. Typed messages = silence.
  if (!body.includes(SCAN_MARKER)) { return callback(null, twiml); }
  try {
    const from = (event.From || "").replace("whatsapp:", "").trim();
    const venue = resolveVenue(event.To, context);
    if (!from || !venue) { return callback(null, twiml); }
    const client = context.getTwilioClient();
    const svc = context.SYNC_SERVICE_SID;
    const mapName = `cards_${venue.id}`;
    await ensureMap(client, svc, mapName);
    let card = (await getCard(client, svc, mapName, from)) || { phone: from, stamps: 0, total_scans: 0, rewards_earned: 0, pending_reward: false, created_at: nowIso() };
    const first = card.total_scans === 0;
    card.stamps += 1; card.total_scans += 1; card.updated_at = nowIso();
    let message;
    if (card.stamps >= venue.stamps_required) {
      card.rewards_earned = (card.rewards_earned || 0) + 1; card.pending_reward = true; card.stamps = 0;
      message = completeMsg(venue);
    } else { message = first ? welcomeMsg(venue, card) : stampMsg(venue, card); }
    await putCard(client, svc, mapName, from, card);
    twiml.message(message);
  } catch (e) { /* stay silent on errors too */ }
  return callback(null, twiml);
};
