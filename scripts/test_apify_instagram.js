#!/usr/bin/env node
/**
 * Test the Apify Instagram scraper against a target profile.
 *
 * Requires APIFY_TOKEN in the environment. Runs the official
 * apify/instagram-scraper Actor, waits for completion, fetches dataset items,
 * and prints a summary. On failure, classifies the cause.
 */

const { ApifyClient } = require('apify-client');

const ACTOR_ID = 'apify/instagram-scraper';
const PROFILE_URL = 'https://www.instagram.com/anatummyisb/';

const INPUT = {
  resultsType: 'posts',
  directUrls: [PROFILE_URL],
  resultsLimit: 10,
};

function fail(category, detail) {
  console.error('\n=== APIFY TEST FAILED ===');
  console.error(`category: ${category}`);
  console.error(`error: ${detail}`);
  process.exit(1);
}

async function main() {
  const token = process.env.APIFY_TOKEN;
  if (!token) {
    fail('missing APIFY_TOKEN', 'APIFY_TOKEN is not set in the environment.');
  }

  const client = new ApifyClient({ token });

  let run;
  try {
    console.log(`Starting Actor ${ACTOR_ID} for ${PROFILE_URL} ...`);
    run = await client.actor(ACTOR_ID).call(INPUT);
  } catch (err) {
    const status = err.statusCode || err.status;
    const msg = err.message || String(err);
    // Egress-proxy rejections surface as 403 too, so check them BEFORE auth.
    if (/not in allowlist|egress|allowlist|blocked by proxy/i.test(msg)) {
      fail('network egress issue',
        `Host blocked by the environment's network egress policy: ${msg}`);
    }
    if (/ENOTFOUND|EAI_AGAIN|ECONNREFUSED|ETIMEDOUT|ECONNRESET|network|socket/i.test(msg)) {
      fail('network egress issue', `Could not reach Apify API: ${msg}`);
    }
    if (status === 401 || status === 403 || /token|unauthor|forbidden/i.test(msg)) {
      fail('Apify auth issue', `${status || ''} ${msg}`.trim());
    }
    if (status === 404 || /actor.*not found|no such actor/i.test(msg)) {
      fail('actor/input issue', `Actor not found or unavailable: ${msg}`);
    }
    if (status === 400 || /invalid input|input.*invalid|schema/i.test(msg)) {
      fail('actor/input issue', `Invalid input: ${msg}`);
    }
    if (/ENOTFOUND|EAI_AGAIN|ECONNREFUSED|ETIMEDOUT|ECONNRESET|network|socket/i.test(msg)) {
      fail('network egress issue', `Could not reach Apify API: ${msg}`);
    }
    fail('actor/input issue', `${status || ''} ${msg}`.trim());
  }

  console.log(`run id:        ${run.id}`);
  console.log(`dataset id:    ${run.defaultDatasetId}`);
  console.log(`run status:    ${run.status}`);

  if (run.status !== 'SUCCEEDED') {
    // Distinguish a profile problem from an actor/input problem where possible.
    fail(
      'actor/input issue',
      `Run finished with status ${run.status}. Check the run log at ` +
        `https://console.apify.com/actors/runs/${run.id} . If the profile is ` +
        `private, removed, or has no posts, that is an Instagram profile issue.`,
    );
  }

  let items;
  try {
    ({ items } = await client.dataset(run.defaultDatasetId).listItems());
  } catch (err) {
    fail('network egress issue', `Failed to fetch dataset items: ${err.message || err}`);
  }

  console.log(`results:       ${items.length}`);

  if (items.length === 0) {
    fail(
      'Instagram profile issue',
      'Actor succeeded but returned 0 items. The profile may be private, ' +
        'have no posts, be unavailable, or Instagram may have blocked the scrape.',
    );
  }

  console.log('\n=== first 3 results ===');
  for (const it of items.slice(0, 3)) {
    console.log(JSON.stringify({
      url: it.url,
      caption: it.caption,
      timestamp: it.timestamp,
      likesCount: it.likesCount,
      commentsCount: it.commentsCount,
    }, null, 2));
  }
}

main().catch((err) => {
  fail('actor/input issue', err.stack || err.message || String(err));
});
