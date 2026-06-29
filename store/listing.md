# Chrome Web Store listing — copy & form answers

Paste these into the Web Store developer dashboard fields.

## Name
surplus — your relationship book, on the side

## Summary (132 chars max)
Keep your surplus book open beside LinkedIn and capture the people you view into your relationships with one click.

## Description
surplus puts your relationship book right next to your browser. Open the side
panel and your surplus "today" view travels with you.

While you browse LinkedIn, surplus shows you who you are looking at and lets you
capture them into your book in one click — surplus then enriches the contact and
drafts a warm message, so your follow-up is ready when you are.

- Your surplus book, pinned in a side panel
- See the LinkedIn profile you are viewing, live
- Capture a profile into surplus with one click
- The contact and a first draft are created for you

You stay signed in through your existing surplus session. surplus only sends a
profile to your account when you click Capture.

## Category
Productivity

## Single purpose (required field)
Show the user's surplus relationship book in a side panel and let them capture
the LinkedIn profile they are viewing into their surplus account.

## Permission justifications
- **host permission `*.linkedin.com`**: Read the name, headline, and URL of the
  LinkedIn profile the user is currently viewing, so it can be shown in the
  panel and captured on request.
- **host permission `event.surpluslayer.com`**: Display the user's surplus book
  in the side panel and send a captured profile to the user's surplus account.
- **sidePanel**: Render the surplus book and context bar as a side panel.
- **scripting**: Inject the profile reader into LinkedIn tabs.
- **tabs**: Detect which LinkedIn page is in front so the panel reflects it.
- **storage**: Lightweight extension state.
- **declarativeNetRequestWithHostAccess**: Allow the surplus book to be embedded
  in the side panel.

## Data use disclosures (Web Store form)
- Personally identifiable information: **Yes** — name + LinkedIn profile of the
  person the user chooses to capture.
- Is data sold to third parties: **No**
- Is data used/transferred for purposes unrelated to the core function: **No**
- Is data used for creditworthiness / lending: **No**
- Data is sent only to the user's own surplus account; not to any third party.

## Privacy policy URL
Host store/privacy-policy.md somewhere public and put the URL here, e.g.
https://surpluslayer.com/extension-privacy

## Visibility recommendation
Unlisted — installable by anyone with the link, not publicly searchable. Best
for a private beta. Switch to Public later if you want discovery.
