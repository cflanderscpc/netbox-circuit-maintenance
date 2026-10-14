__all__ = ('split_provider_emails',)


def split_provider_emails(value):
    """
    Return the sender addresses in a Provider's emails custom field value, stripped and lower-cased as the mail
    sources match senders. Empty entries are skipped, and a value that isn't a string (the field's type was changed)
    has no addresses.
    """
    if not isinstance(value, str):
        return []
    return [email for email in (entry.strip().lower() for entry in value.split(',')) if email]
