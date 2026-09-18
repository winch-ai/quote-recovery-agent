"""Public privacy and terms pages.

Meta will not let an app leave Development mode without a Privacy Policy URL,
and there is no domain yet, so these are served from the Cloud Run service
itself. The text is deliberately accurate about a pilot rather than boilerplate
copied from a company that does not exist.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

_STYLE = """
:root { color-scheme: light dark; }
body { font: 16px/1.65 system-ui, -apple-system, Segoe UI, sans-serif;
       max-width: 46rem; margin: 0 auto; padding: 3rem 1.25rem 5rem; }
h1 { font-size: 1.6rem; margin-bottom: .25rem; }
h2 { font-size: 1.05rem; margin-top: 2.25rem; }
.sub { opacity: .65; font-size: .9rem; margin-top: 0; }
ul { padding-left: 1.2rem; } li { margin: .3rem 0; }
footer { margin-top: 3rem; font-size: .85rem; opacity: .65; }
"""


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=en><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>{_STYLE}</style>{body}")


def build_router(contact_email: str, operator_name: str,
                 updated: str = "18 September 2026") -> APIRouter:
    router = APIRouter()
    contact = (f'<a href="mailto:{contact_email}">{contact_email}</a>'
               if contact_email else "the operator of this service")

    @router.get("/privacy", response_class=HTMLResponse)
    async def privacy() -> str:
        return _page("Privacy Policy", f"""
<h1>Privacy Policy</h1>
<p class=sub>Last updated {updated}</p>

<p>This service helps a trade contractor follow up on quotes they have already
sent. It is operated by {operator_name} and is currently running as a limited
pilot with a small number of participating businesses.</p>

<h2>What we process</h2>
<ul>
  <li><strong>Quote documents</strong> that a contractor sends us — PDFs or
      photographs of written quotes.</li>
  <li><strong>Customer contact details</strong> contained in or supplied
      alongside those quotes: name, phone number and, where given, email.</li>
  <li><strong>Messages</strong> exchanged between the contractor, this service
      and the customer, including delivery status.</li>
</ul>

<h2>Why</h2>
<p>Solely to send the follow-up messages the contractor has approved, and to
show the contractor the replies. We do not use this information to build
profiles, we do not sell it, and we do not share it with third parties for
their own purposes.</p>

<h2>Controller and processor</h2>
<p>The contractor is the controller of their customers' personal data. This
service acts as a processor on their instructions. Every message sent to a
customer is individually approved by the contractor before it leaves the
system.</p>

<h2>Who else sees it</h2>
<ul>
  <li><strong>Meta Platforms</strong> — messages are delivered over the
      WhatsApp Business Platform and are subject to Meta's own terms.</li>
  <li><strong>Microsoft Azure OpenAI</strong> — quote documents are processed
      to extract structured details such as the customer name and total.</li>
  <li><strong>Google Cloud</strong> — hosting and database storage, in the
      United Kingdom (europe-west2).</li>
</ul>

<h2>Retention</h2>
<p>Quote and message records are kept for the duration of the pilot and deleted
within 30 days of it ending, or sooner on request.</p>

<h2>Your rights</h2>
<p>Under the UK and EU GDPR you may request access to, correction of, or
deletion of your personal data, and may object to its processing. Contact
{contact}, or reply <strong>STOP</strong> to any message to be removed
immediately and contacted no further.</p>

<h2>Contact</h2>
<p>{contact}</p>
<footer>This page is served by the application itself and reflects what the
software actually does.</footer>""")

    @router.get("/data-deletion", response_class=HTMLResponse)
    async def data_deletion() -> str:
        """Meta requires a Data Deletion Instructions URL before an app goes Live.

        Kept as a separate page from /privacy because Meta's reviewers look for
        a page whose entire subject is deletion, not a section inside a longer
        policy.
        """
        return _page("Data Deletion Instructions", f"""
<h1>How to delete your data</h1>
<p class=sub>Last updated {updated}</p>

<p>This service sends quote follow-up messages on behalf of a trade contractor.
If you have received a message from it, or your details were included in a
quote processed by it, you can have that data removed.</p>

<h2>Fastest: reply STOP</h2>
<p>Reply <strong>STOP</strong> to any message you have received. This stops all
further messages to you immediately and marks your record for deletion. You do
not need to explain why, and nobody will contact you to ask.</p>

<h2>By email</h2>
<p>Write to {contact} with the phone number or email address that received the
message. Deletion is completed within 30 days, and normally within a few
working days.</p>

<h2>What gets deleted</h2>
<ul>
  <li>Your name, phone number and email address.</li>
  <li>The quote details associated with you.</li>
  <li>The message history between you and this service.</li>
</ul>

<h2>What may remain</h2>
<p>Messages already delivered over WhatsApp remain in Meta's systems and in the
recipients' own devices and chat history; that is outside this service's
control. The contractor also keeps their own copy of the quote, which is their
business record and is not ours to delete — contact them directly about it.</p>

<h2>Contact</h2>
<p>{contact}</p>
<footer>See also the <a href="/privacy">Privacy Policy</a>.</footer>""")

    @router.get("/terms", response_class=HTMLResponse)
    async def terms() -> str:
        return _page("Terms of Service", f"""
<h1>Terms of Service</h1>
<p class=sub>Last updated {updated}</p>

<p>This service is provided to participating contractors as a pilot, free of
charge, and without warranty of any kind.</p>

<h2>What it does</h2>
<p>It reads quotes a contractor sends it, proposes a follow-up sequence, and
sends approved messages to that contractor's customers. <strong>No message is
sent to a customer without the contractor approving it first.</strong></p>

<h2>Responsibility for content</h2>
<p>The contractor is responsible for the accuracy of their quotes and for the
messages they approve. Prices, dates and scope are carried through from the
contractor's own document and are never generated by this service.</p>

<h2>Availability</h2>
<p>This is pilot software. It may be unavailable, may be withdrawn at any time,
and should not be relied upon as the only record of a quote.</p>

<h2>Contact</h2>
<p>{contact}</p>""")

    return router
