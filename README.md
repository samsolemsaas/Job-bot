# Hallway Track

Find the buyer conversations your company should be part of.

Hallway Track listens to buyers, not competitors. Every morning it reads public conversations where security buyers ask for help, then tells the team which ones to join, who should answer, what point of view to bring and which rooms to show up in.

Built by Samantha Solemsaas.

## What it answers

- **Which conversations should we weigh in on today?** Each one is scored on whether it's a problem the company can speak to, who is asking, whether anyone has answered and how fresh it is. Every card shows the owner, the point of view to bring, the room's posting norms and a draft reply.
- **Which topics should we own?** Buyer problems ranked by fit, volume, momentum and unanswered questions, with buyer quotes and the threads still waiting for a good answer.
- **What's emerging?** Once a week AI groups conversations that don't fit any known topic, so the team can name a problem before anyone else.
- **Which rooms should our people be in?** Communities ranked by audience fit and where the conversation actually happens, with who should join and how to show up.
- **What words do buyers use?** Phrases they repeat, for content, ads and talk tracks.

## Lenses

Listening is neutral. A lens is one company's view of it: who it sells to, the buyer problems it can speak to (core, adjacent or listen-only), the point of view it brings and who should say it. Lenses live in `lenses/`. Nagomi (exposure management) and Ocean (email security) are included, and the dashboard switches between them.

To add a company, copy a lens file, edit it and add its name to `lenses` in `config.toml`.

## How it works

1. **Listen.** Hacker News, Information Security Stack Exchange, infosec.exchange, Google News and, with free API keys, Reddit.
2. **Read through each lens.** Tag the buyer, the problem and whether they're asking or shopping.
3. **Score.** Rules in `config.toml` under `[weigh_in]` decide what's worth a reply. Every card lists the rules that fired.
4. **Draft.** GitHub Models (free) writes replies, names emerging topics and writes a weekly brief per lens.
5. **Publish.** Everything lands in `hallway.json`, and `index.html` is the dashboard on GitHub Pages.

## Privacy

Public posts only. It never joins or reads private groups, it doesn't store usernames, and every excerpt links to the original.

## Cost

$0. GitHub Actions, GitHub Pages and the GitHub Models free tier cover it. Reddit is optional and uses Reddit's free API.
