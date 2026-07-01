"""Public buyer tours for the hosted Hestia Studio offer."""

DEMO_TOURS = {
    "wedding": {
        "label": "Wedding",
        "headline": "Wedding studio command center",
        "summary": (
            "From first inquiry to booked date, signed agreement, paid retainer, "
            "gallery delivery, album offer, and anniversary rebooking."
        ),
        "accent": "wedding",
        "before": "HoneyBook + contract app + gallery host + invoice tool + email reminders",
        "workflow": [
            ("Inquiry", "A couple lands on the studio site and becomes a CRM lead."),
            ("Booking", "The owner sends one package-backed proposal with agreement and retainer."),
            ("Shoot", "Questionnaire, schedule, contract, and payment stay attached to the project."),
            ("Gallery", "Hestia delivers the gallery and builds an AI-curated print and album offer."),
            ("Retention", "Review, reconnect, and proposal follow-up automations keep revenue moving."),
        ],
        "proof": [
            "Proposal view tracking shows whether they opened the booking link.",
            "Accepted proposals point the owner to signature and payment gaps.",
            "Gallery-to-offer workflow keeps print and album revenue in the same loop.",
        ],
    },
    "food": {
        "label": "Food & beverage",
        "headline": "F&B content studio command center",
        "summary": (
            "Handle restaurant inquiries, campaign shot lists, reusable packages, "
            "proofing, usage-ready galleries, invoices, and follow-up content work."
        ),
        "accent": "food",
        "before": "Airtable + file delivery + invoice app + proofing comments + email threads",
        "workflow": [
            ("Lead", "A restaurant or brand inquiry becomes a project with source tracking."),
            ("Package", "Menu, launch, and social content packages become proposals and invoices."),
            ("Production", "Shot lists, notes, files, forms, and schedule live on the project."),
            ("Delivery", "Client proofing, favorites, comments, and final delivery stay in one portal."),
            ("Retention", "Campaign reminders and reconnect surfaces create the next booking."),
        ],
        "proof": [
            "Packages make recurring content retainers fast to quote.",
            "Proofing comments and favorites reduce scattered feedback.",
            "Client statements keep usage, invoices, and paid work visible.",
        ],
    },
    "real-estate": {
        "label": "Real estate",
        "headline": "Real-estate media command center",
        "summary": (
            "Turn listing inquiries into bookings, collect property details, deliver "
            "fast galleries, invoice agents, and keep repeat listing work warm."
        ),
        "accent": "estate",
        "before": "Scheduling tool + forms + Dropbox + invoice app + follow-up spreadsheet",
        "workflow": [
            ("Request", "An agent requests a listing shoot from the public booking path."),
            ("Details", "Property address, access notes, and deliverables are collected up front."),
            ("Booking", "Confirmed sessions send calendar links, reminders, and deposit invoices."),
            ("Delivery", "The gallery and downloads are tracked with view and delivery signals."),
            ("Repeat", "Reconnect surfaces identify agents ready for their next listing."),
        ],
        "proof": [
            "Booking rules prevent last-minute surprises.",
            "Gallery engagement shows whether the agent opened or downloaded the delivery.",
            "Receivables and reminders keep listing invoices from going stale.",
        ],
    },
}


def demo_tour(key: str | None) -> dict:
    normalized = (key or "wedding").strip().lower().replace("_", "-")
    if normalized == "real_estate":
        normalized = "real-estate"
    if normalized not in DEMO_TOURS:
        normalized = "wedding"
    tour = dict(DEMO_TOURS[normalized])
    tour["key"] = normalized
    return tour


def demo_nav() -> list[dict]:
    return [{"key": key, "label": tour["label"]} for key, tour in DEMO_TOURS.items()]
