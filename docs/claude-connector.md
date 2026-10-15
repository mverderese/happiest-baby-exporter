# Using the Happiest Baby connector in Claude

The connector lets Claude check and control your SNOO and read and write your baby's
journal. It's useful when the Happiest Baby app isn't working. It works in Claude on the
web, on desktop, and in the iPhone and Android apps.

You need the connector URL of a deployed server, `https://<project>.vercel.app/mcp`
(see [Use as a Claude connector](../README.md#use-as-a-claude-connector-remote-mcp-server-with-oauth)
to deploy your own), and your Happiest Baby login, the one you use in the Happiest Baby app.
Accounts that use two-factor sign-in aren't supported.

## Add it to Claude

1. Go to [claude.ai](https://claude.ai) in a browser (on a phone or a computer) and open
   **Settings → Connectors → Add custom connector**. Custom connectors can't be added from
   the mobile app, but once added they show up there too.
2. Name it **Happiest Baby** and paste the connector URL. Leave the OAuth client ID and
   secret empty.
3. Click **Connect**. On the page that opens:
   - Enter your Happiest Baby email and password.
   - Check the **Time zone** field. It's filled in from your browser (e.g.
     `America/New_York`). Times you give Claude, like "10:30", are read in this zone.
   - Click **Sign in and allow**. You're sent back to Claude.

Your password is sent to Happiest Baby once to sign in. It isn't stored, and Claude never
sees it.

**Claude Code:** connectors added at claude.ai also appear in Claude Code. To add it to
Claude Code only, run `claude mcp add --transport http happiest-baby <connector URL>`,
then `/mcp` to sign in.

## Use it

Turn on the Happiest Baby connector in a chat and ask things like:

- "What's the SNOO status?"
- "Put the SNOO on level 2 and hold it there." / "Stop the SNOO."
- "Turn on white noise for 30 minutes."
- "How did the baby sleep last night?"
- "Log a wet diaper at 3:10."

Claude asks before it changes anything. It also passes on a warning if a safety clip
isn't attached.

Sleep includes naps logged in the app outside the SNOO, with their tag (such as "in arms")
and note. Logging feeds, pumping, and solid food is experimental: Claude copies the format
of an entry you've already logged in the app, so log one of that type in the app first.

## Disconnecting

- **Remove it from Claude:** Settings → Connectors → Happiest Baby → Disconnect.
- **Change your time zone:** disconnect and connect again.
- **If Claude asks you to reconnect:** your sign-in expired. Connect again and sign in.
