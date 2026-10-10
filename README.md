# Discord Bonus Bot (Grand RP)

A `discord.py` bot for event screenshot channels. All six configured events use **50,000 per kill** by default. A different explicitly stated rate (for example `200k per kill`) is sent to an approval channel. Only members with role ID `1525107899381186709` or a Discord administrator can approve/reject it.

## Important behavior

- Post an image/screenshot in an event channel.
- For reliable calculations, include Discord member mentions and kill counts in the same message:
  - `@Member 2k` means **2 kills**.
  - `@Member 1k` means **1 kill**.
- The bot calculates `kills × event rate`.
- A custom rate such as `200k per kill` must be approved every time it differs from the 50k default. Approval/rejection buttons are in channel `1525131055412154368`.
- If a screenshot has no readable member-count list, the bot sends it to `1548222840023678976` for manual verification.
- OCR text is advisory only. It cannot safely determine which game name belongs to which Discord member, so it will not automatically award money from OCR alone.
- SQLite stores submission and approval records. For Railway, attach a persistent volume and set `DATABASE_PATH=/data/bonus_bot.sqlite3`.

## Discord Developer Portal setup

1. Create an application at https://discord.com/developers/applications and add a Bot.
2. Copy the bot token. Never share it or commit it to source control.
3. Under **Bot → Privileged Gateway Intents**, enable:
   - **Message Content Intent**
   - **Server Members Intent**
4. Invite it to your server with scopes `bot` and `applications.commands` (bot commands use prefix `!`).
5. Suggested permissions: View Channels, Send Messages, Read Message History, Embed Links, Attach Files. Make sure it can view/send messages in all event, approval, and manual-review channels.
6. Give the bot role enough channel permissions; put its role above any roles it must interact with only if needed. The bot does not assign roles.

## Railway deployment

1. Create a new Railway project and deploy this folder from a GitHub repository.
2. Add variables:
   - `DISCORD_TOKEN` = your bot token
   - `DATABASE_PATH` = `/data/bonus_bot.sqlite3` (if using the volume below)
3. Add a Railway Volume mounted at `/data`. This is important so the SQLite database survives redeploys.
4. The included `Procfile` runs `python bot.py`.
5. Deploy and check logs for `Logged in as ...`.
6. In Discord, run `!bonushelp` to check the bot is responding. An administrator can run `!bonusstatus`.

### OCR note

The bot works without OCR, but screenshot transcription is better when Tesseract is installed. `nixpacks.toml` requests Tesseract on Railway. OCR is never treated as final proof or used to auto-pay without a clear Discord member/count mapping.

## Channel and rate configuration

Configured in `bot.py`:

| Event | Channel ID | Default |
|---|---:|---:|
| State-Win | 1525477519451619349 | 50k/kill |
| Informal-Win | 1525134079605997678 | 50k/kill |
| Weapon-Factory | 1525131417854410772 | 50k/kill |
| Foundry | 1525131463492636714 | 50k/kill |
| Biz-War | 1525131518039818350 | 50k/kill |
| Rating-Battle | 1525131566802665552 | 50k/kill |

Approval channel: `1525131055412154368`  
Manual-review channel: `1548222840023678976`  
Approver role: `1525107899381186709`

## Example

Message in an event channel with screenshot:

`@Aekal Tnt 2k`
`@Karim Cho 1k`

At 50k per kill, the bot replies:
- Aekal: 2 × 50k = 100k
- Karim: 1 × 50k = 50k
- Total: 150k

Custom rate example: `200k per kill` with the screenshot. The bot sends an approval card. On approval, it calculates using 200k; on rejection, it uses the default 50k.

## Security and limitations

- Keep the token in Railway Variables only.
- Review custom-rate requests before approval; the bot trusts the submitted screenshot/message as evidence, not as an unquestionable source.
- Do not grant the approver role to untrusted users.
- A `k` after a member mention means **kills** in this bot's input format, not a money amount.
- The bot doesn't infer Discord identities from in-game leaderboard names. For screenshot-only posts, it asks staff to verify rather than risk paying the wrong person.
