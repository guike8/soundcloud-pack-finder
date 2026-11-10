# Anonymous SoundCloud website adapter

Observed on 2026-11-10 against the user-supplied public profile. This documents the website's anonymous protocol, not the registered SoundCloud developer API.

1. GET the public profile HTML with a normal browser User-Agent.
2. Parse `window.__sc_hydration` as JSON. The `apiClient` entry supplies the public website client identifier, and the `user` entry supplies the starting profile. No account login or cookies are read.
3. Website JavaScript declares `api_v2_host` as `https://api-v2.soundcloud.com`, a user-followers route (`users/:id/followers`), and external-profile links (`users/:urn/web-profiles`).
4. Send anonymous requests with the public `client_id` query parameter, `Accept: application/json`, the SoundCloud `Origin`, profile `Referer`, and browser User-Agent. There is no `Authorization` header.
5. Follow `next_href` with the same dynamic client identifier. Validate the host before sending it, and deduplicate users.

The implementation discovers the identifier at runtime. It does not publish a captured identifier, ship a secret, reuse someone else's API app credentials, or fetch signed-in browser storage. HTML hydration and public endpoints were checked with successful anonymous live requests. REA static inspection of the shipped scripts supplied additional source evidence; one large bundle exceeded the selected analysis budget, so this does not claim a complete reconstruction of SoundCloud.

Only public profiles and public follower information are in scope. Stop on authentication barriers, challenges, or access denials. Site changes can break this unofficial route; the adapter raises a clear error instead of falling back to private account data.
