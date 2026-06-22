/**
 * Inbound WhatsApp loyalty handler — runs ON Twilio (Twilio Functions).
 *
 * No server, no Render, no ngrok: Twilio hosts this code and Twilio Sync stores
 * the stamp cards. Point each WhatsApp number's "when a message comes in"
 * webhook at this function's URL.
 *
 * Each scan = one inbound message. We add a stamp, save the card to a Sync Map
 * (one map per restaurant, item key = the customer's phone number), and reply.
 * Identity is the customer's WhatsApp number; no signup, no name.
 */

function nowIso() {
  return new Date().toISOString();
}

function bar(stamps, required) {
  const filled = "▰".repeat(Math.max(0, stamps));
  const empty = "▱".repeat(Math.max(0, required - stamps));
  return `[${filled}${empty}] ${stamps}/${required}`;
}

function resolveVenue(to, context) {
  const digits = (to || "").replace(/\D/g, "");
  let list = [];
  try {
    list = JSON.parse(context.RESTAURANTS || "[]");
  } catch (e) {
    list = [];
  }
  if (list.length) {
    const found = list.find(
      (v) => (v.whatsapp_number || "").replace(/\D/g, "") === digits
    );
    if (found) return normalize(found);
    if (list.length === 1) return normalize(list[0]);
    return null;
  }
  // Single-venue fallback from plain env vars.
  return normalize({
    id: context.VENUE_ID || "default",
    name: context.VENUE_NAME || "Sugar Rush",
    stamps_required: context.STAMPS_REQUIRED,
    reward: context.REWARD,
  });
}

function normalize(v) {
  return {
    id: v.id,
    name: v.name || "Rewards",
    stamps_required: parseInt(v.stamps_required, 10) || 5,
    reward: v.reward || "a free treat",
  };
}

async function getCard(client, svc, mapName, phone) {
  try {
    const item = await client.sync.v1
      .services(svc)
      .syncMaps(mapName)
      .syncMapItems(phone)
      .fetch();
    return item.data;
  } catch (e) {
    return null;
  }
}

async function ensureMap(client, svc, mapName) {
  try {
    await client.sync.v1.services(svc).syncMaps(mapName).fetch();
  } catch (e) {
    await client.sync.v1.services(svc).syncMaps.create({ uniqueName: mapName });
  }
}

async function putCard(client, svc, mapName, phone, data) {
  try {
    await client.sync.v1
      .services(svc)
      .syncMaps(mapName)
      .syncMapItems(phone)
      .update({ data });
  } catch (e) {
    await client.sync.v1
      .services(svc)
      .syncMaps(mapName)
      .syncMapItems.create({ key: phone, data });
  }
}

function welcomeMsg(venue, card) {
  return (
    `🎉 Welcome to ${venue.name} Rewards!\n` +
    `You earned your 1st stamp on your Loyalty card.\n` +
    `${bar(card.stamps, venue.stamps_required)}\n` +
    `Collect ${venue.stamps_required} stamps and ${venue.reward} is on us — ` +
    `scan the QR on every visit to fill it up!`
  );
}

function stampMsg(venue, card) {
  const remaining = venue.stamps_required - card.stamps;
  const nudge =
    remaining === 1
      ? "Just 1 more to go!"
      : `${remaining} more and you've earned ${venue.reward}.`;
  return (
    `⭐ Stamp added at ${venue.name}!\n` +
    `${bar(card.stamps, venue.stamps_required)}\n` +
    `${nudge} See you soon!`
  );
}

function completeMsg(venue) {
  return (
    `🏆 Card complete at ${venue.name}! You've unlocked ${venue.reward.toUpperCase()}.\n` +
    `Show this message to our staff to claim it. 🎁\n` +
    `Your card has reset — collect ${venue.stamps_required} more for ${venue.reward} again!\n` +
    `${bar(0, venue.stamps_required)}`
  );
}

exports.handler = async function (context, event, callback) {
  const twiml = new Twilio.twiml.MessagingResponse();
  try {
    const from = (event.From || "").replace("whatsapp:", "").trim();
    const venue = resolveVenue(event.To, context);
    if (!from || !venue) {
      twiml.message("Sorry, we couldn't process that scan — please try again.");
      return callback(null, twiml);
    }

    const client = context.getTwilioClient();
    const svc = context.SYNC_SERVICE_SID;
    const mapName = `cards_${venue.id}`;
    await ensureMap(client, svc, mapName);

    let card =
      (await getCard(client, svc, mapName, from)) || {
        phone: from,
        stamps: 0,
        total_scans: 0,
        rewards_earned: 0,
        pending_reward: false,
        created_at: nowIso(),
      };

    const first = card.total_scans === 0;
    card.stamps += 1;
    card.total_scans += 1;
    card.updated_at = nowIso();

    let message;
    if (card.stamps >= venue.stamps_required) {
      card.rewards_earned = (card.rewards_earned || 0) + 1;
      card.pending_reward = true;
      card.stamps = 0;
      message = completeMsg(venue);
    } else {
      message = first ? welcomeMsg(venue, card) : stampMsg(venue, card);
    }

    await putCard(client, svc, mapName, from, card);
    twiml.message(message);
  } catch (e) {
    twiml.message("Something went wrong — please try scanning again.");
  }
  return callback(null, twiml);
};
