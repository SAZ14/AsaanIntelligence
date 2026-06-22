/**
 * Inbound WhatsApp loyalty handler — runs ON Twilio (Twilio Functions).
 *
 * A stamp is only granted when the customer's message contains the venue's
 * SECRET CODE — the code lives in the QR that STAFF keep at the counter and
 * show when a customer qualifies. A plain "hi" carries no code, so it never
 * earns a stamp. An optional cooldown stops a memorised code from being
 * re-sent to farm stamps.
 *
 * No server: Twilio hosts this; Twilio Sync stores the cards (one Sync Map per
 * restaurant, item key = the customer's phone number).
 */

function nowIso() { return new Date().toISOString(); }

function bar(stamps, required) {
  const filled = "▰".repeat(Math.max(0, stamps));
  const empty = "▱".repeat(Math.max(0, required - stamps));
  return `[${filled}${empty}] ${stamps}/${required}`;
}

function normalize(v) {
  return {
    id: v.id,
    name: v.name || "Rewards",
    stamps_required: parseInt(v.stamps_required, 10) || 5,
    reward: v.reward || "a free treat",
    code: (v.code || "").toString().trim(),
    cooldown_hours: parseInt(v.cooldown_hours, 10) || 0,
  };
}

function resolveVenue(to, context) {
  const digits = (to || "").replace(/\D/g, "");
  let list = [];
  try { list = JSON.parse(context.RESTAURANTS || "[]"); } catch (e) { list = []; }
  if (list.length) {
    const found = list.find((v) => (v.whatsapp_number || "").replace(/\D/g, "") === digits);
    if (found) return normalize(found);
    if (list.length === 1) return normalize(list[0]);
    return null;
  }
  return normalize({
    id: context.VENUE_ID || "default",
    name: context.VENUE_NAME || "Sugar Rush",
    stamps_required: context.STAMPS_REQUIRED,
    reward: context.REWARD,
    code: context.CODE,
    cooldown_hours: context.COOLDOWN_HOURS,
  });
}

async function getCard(client, svc, mapName, phone) {
  try {
    const item = await client.sync.v1.services(svc).syncMaps(mapName).syncMapItems(phone).fetch();
    return item.data;
  } catch (e) { return null; }
}

async function ensureMap(client, svc, mapName) {
  try { await client.sync.v1.services(svc).syncMaps(mapName).fetch(); }
  catch (e) { await client.sync.v1.services(svc).syncMaps.create({ uniqueName: mapName }); }
}

async function putCard(client, svc, mapName, phone, data) {
  try { await client.sync.v1.services(svc).syncMaps(mapName).syncMapItems(phone).update({ data }); }
  catch (e) { await client.sync.v1.services(svc).syncMaps(mapName).syncMapItems.create({ key: phone, data }); }
}

function noCodeMsg(v) {
  return `👋 To collect a stamp at ${v.name}, scan the code our staff shows you at the counter. See you soon!`;
}
function cooldownMsg(v, c) {
  return `You've already collected a stamp recently — see you on your next visit! ${bar(c.stamps, v.stamps_required)}`;
}
function welcomeMsg(v, c) {
  return `🎉 Welcome to ${v.name} Rewards!\nYou earned your 1st stamp on your Loyalty card.\n${bar(c.stamps, v.stamps_required)}\nCollect ${v.stamps_required} stamps and ${v.reward} is on us!`;
}
function stampMsg(v, c) {
  const r = v.stamps_required - c.stamps;
  const nudge = r === 1 ? "Just 1 more to go!" : `${r} more and you've earned ${v.reward}.`;
  return `⭐ Stamp added at ${v.name}!\n${bar(c.stamps, v.stamps_required)}\n${nudge} See you soon!`;
}
function completeMsg(v) {
  return `🏆 Card complete at ${v.name}! You've unlocked ${v.reward.toUpperCase()}.\nShow this message to our staff to claim it. 🎁\nYour card has reset — collect ${v.stamps_required} more for ${v.reward} again!\n${bar(0, v.stamps_required)}`;
}

exports.handler = async function (context, event, callback) {
  const twiml = new Twilio.twiml.MessagingResponse();
  try {
    const from = (event.From || "").replace("whatsapp:", "").trim();
    const venue = resolveVenue(event.To, context);
    if (!from || !venue) { twiml.message("Sorry, we couldn't process that."); return callback(null, twiml); }

    // A stamp requires the venue's secret code (from the staff-held QR).
    const body = (event.Body || "").toUpperCase();
    if (venue.code && !body.includes(venue.code.toUpperCase())) {
      twiml.message(noCodeMsg(venue));
      return callback(null, twiml);
    }

    const client = context.getTwilioClient();
    const svc = context.SYNC_SERVICE_SID;
    const mapName = `cards_${venue.id}`;
    await ensureMap(client, svc, mapName);

    let card = (await getCard(client, svc, mapName, from)) ||
      { phone: from, stamps: 0, total_scans: 0, rewards_earned: 0, pending_reward: false, created_at: nowIso() };

    // Cooldown: stop a memorised code from being re-sent to farm stamps.
    if (venue.cooldown_hours > 0 && card.updated_at) {
      const hrs = (Date.now() - new Date(card.updated_at).getTime()) / 3600000;
      if (hrs < venue.cooldown_hours) {
        twiml.message(cooldownMsg(venue, card));
        return callback(null, twiml);
      }
    }

    const first = card.total_scans === 0;
    card.stamps += 1; card.total_scans += 1; card.updated_at = nowIso();
    let message;
    if (card.stamps >= venue.stamps_required) {
      card.rewards_earned = (card.rewards_earned || 0) + 1; card.pending_reward = true; card.stamps = 0;
      message = completeMsg(venue);
    } else { message = first ? welcomeMsg(venue, card) : stampMsg(venue, card); }
    await putCard(client, svc, mapName, from, card);
    twiml.message(message);
  } catch (e) { twiml.message("Something went wrong — please try again."); }
  return callback(null, twiml);
};
