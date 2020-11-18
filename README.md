# dreamsmp

Play/streaming times for all the DreamSMP members

## Setup

Env variables:
- `ip` = The Minecraft server IP you wish to track
- `dburi` = MongoDB uri (can be aquired for free at MongoDB.com)
- `twitch_client_id` and `twitch_token` = Your Twitch API client id and token

Make sure to set `youtube` and/or `twitch` values as the user's YouTube/Twitch IDs in the `players` collection (Twitch ids are numerical, YouTube IDs are what comes after the /c/ in the url)
