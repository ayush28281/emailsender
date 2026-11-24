# Emailsender

A personal email sender for job, startup, referral, and freelance outreach. Save your work profile and connect a mailbox once. After that, add a recipient’s address to research their company, prepare a message, and start a reply-aware sequence.

## Run it locally

Requires Python 3.11 or newer. The app uses the Python standard library.

On Windows, double-click **Start Emailsender.cmd**. From a terminal in this folder, run

```sh
python -m outreach --open
```

Open **Sender setup** in the dashboard.

1. Save your name, role, outreach goal, a specific contribution you made, and a public link showing that work. Add a résumé link if you want to handle résumé requests. For freelance outreach, you can save a service proposal in your own words.
2. Choose **Connect mailbox** and enter your sending address and app password. The app tests SMTP and IMAP authentication before saving the connection. Gmail users with two-step verification can use an app password when their account supports it. [Google’s app password instructions](https://support.google.com/mail/answer/185833)
3. Open **Queue**, check the sending identity and goal shown above the recipient field, enter the recipient’s email, and choose **Start outreach**. A ready email can go out immediately. For a social DM, the action prepares a copyable draft.

The dashboard runs at [127.0.0.1:8765](http://127.0.0.1:8765/). Keep the process and computer running for inbox checks and scheduled sends. You can close the browser tab. Choose **Quit local app** in Sender setup to stop the worker. On restart, it checks the inbox before processing overdue messages.

## How it chooses a message

The app uses your goal, the recipient’s address, any details you supply, and public information on the company’s own site. Contact details show the chosen scenario, the research source when available, the reason for any hold, and the composed message.

| Situation | What happens |
| --- | --- |
| Startup outreach | A company-specific opening introduces your work and asks about contributing to the team. The app uses a neutral greeting when the recipient’s name is unknown. |
| Advertised role | The app looks for an exact job-title link on the company or careers page. You can supply the current posting and title directly. Recruiting addresses can select this route automatically. |
| Referral | The email includes the role and posting, your evidence, and one referral request. |
| Freelance | A company observation, your proposal, and a work example lead to a question about sharing the idea. A contact-specific proposal takes precedence over the one in your profile. |
| LinkedIn or X | The queue prepares a short DM for you to copy into the social platform. |
| No reply | One threaded check-in is due after four business days. A final check-in is due five business days later. The sequence then closes. Business days are Monday through Friday in the computer’s local time zone. |

When research needs more information, the dashboard opens the recipient with a focused question. For instance, a referral with a known company and role may need only the job listing. For a personal mailbox such as Gmail, enter the company so the app can find relevant saved details.

When a company match is available, choose **Use saved details** to fill the form, review the observation, then choose **Save and continue**. You can mark an observation you enter as reusable for that company. A recipient-specific note stays with that contact by default. Once the necessary details are saved, a ready email can go out.

## Replies and results

The worker checks the inbox before each send. A human reply ends scheduled follow-ups. An opt-out suppresses that recipient. A delivery failure recorded by the provider marks the address as bounced. When a send’s acceptance is uncertain, the queue holds it for you to check your sent mail.

The optional **Send automatic replies** setting can send one in-thread response to an unambiguous résumé request, experience question, or decline. It uses your saved résumé or work sample as appropriate. Call invitations appear for review with a suggestion when you supplied availability. Mixed or unfamiliar requests stay available for you to answer in your mailbox.

The dashboard tracks contact dates, original messages, follow-ups, replies, and outcomes. Record a call or interview in **Details**. All rates use the number of unique contacts whose initial email was accepted by SMTP, excluding known bounces. This estimates delivery, since mailbox providers do not report inbox placement here.

## Manage your job search

- Open **Jobs** to track openings from saved through applied, interview, and offer. Add the posting link, the next step and its date, and notes. The next-step list brings deadlines and outreach that needs your attention into the same view.
- From a role or referral email, choose **Track this application** to start a job record with the role, company, posting, and recipient already filled in.
- In **Sender setup**, add work samples with skill keywords. When a role or company observation matches an additional sample, the app can use that example in the first email. Open the recipient details to see the sample chosen for the draft.
- Turn on **Review first emails** in **Sender setup** to edit the subject and body of new drafts before approving them. The first email can send after approval. After it sends, the reply and follow-up rules above apply.

## Local configuration

- Gmail defaults are SMTP over TLS on port 465 and IMAP over TLS on port 993. Other providers can use the **Provider settings** fields if they support password-based SMTP and IMAP. SMTP on other ports, including 587, requires STARTTLS.
- As an alternative to the connection form, copy `.env.example` to `.env`, fill in the mailbox settings, and restart. Settings saved by the form take precedence.
- Your queue and profile live in `.emailsender/`. A mailbox password saved by the form uses your Windows account’s data protection. On other systems it is stored in a user-private file. The optional `.env` contains whatever credentials you put in it. Both locations are excluded from Git.
- Each data folder belongs to one sending address. For a second sender, start a separate instance with `python -m outreach --port 8766 --data-dir PATH --open`. Keep its data folder outside version control.
- Preparing messages uses local rules, your saved facts, and public HTTPS pages. The app requires no AI API key or social-media account.

## Verify

```sh
python -m unittest discover -s tests -v
```

The tests use temporary databases and simulated mailboxes. They cover scenario selection, protected company lookups, reply handling, duplicate prevention, scheduling, and the dashboard workflow.

The outreach scenarios and follow-up approach are based on Sumit Kesarwani’s guide. [Cold Email Playbook](https://crackcoldemail.vercel.app/)
