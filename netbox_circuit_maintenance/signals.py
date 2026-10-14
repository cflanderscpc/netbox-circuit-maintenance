from circuit_maintenance_parser import SUPPORTED_PROVIDER_NAMES
from circuits.models import Provider
from django.core.exceptions import ValidationError
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _
from netbox.signals import post_clean

from .constants import PROVIDER_EMAILS_FIELD, PROVIDER_PARSER_FIELD
from .utils import split_provider_emails

__all__ = ('validate_provider',)


@receiver(post_clean, sender=Provider)
def validate_provider(sender, instance, **kwargs):
    """
    Reject a sender address that another Provider already uses, and a parser name that circuit-maintenance-parser
    doesn't support. post_clean is sent before NetBox validates custom field data, so values can be of any type.
    """
    errors = []

    # Each address once, in the order entered
    if emails := dict.fromkeys(split_provider_emails(instance.custom_field_data.get(PROVIDER_EMAILS_FIELD))):
        other_values = (
            Provider.objects.exclude(pk=instance.pk)
            .order_by()
            .values_list(f'custom_field_data__{PROVIDER_EMAILS_FIELD}', flat=True)
        )
        used_emails = {email for value in other_values for email in split_provider_emails(value)}
        errors.extend(
            _("Invalid value for custom field '{name}': {email} is already used by another provider.").format(
                name=PROVIDER_EMAILS_FIELD, email=email
            )
            for email in emails
            if email in used_emails
        )

    # The parser library looks the name up lower-cased too
    parser = instance.custom_field_data.get(PROVIDER_PARSER_FIELD)
    if parser and isinstance(parser, str) and parser.lower() not in SUPPORTED_PROVIDER_NAMES:
        errors.append(
            _(
                "Invalid value for custom field '{name}': {parser} is not one of the supported providers in the "
                'circuit-maintenance-parser library: {supported}.'
            ).format(name=PROVIDER_PARSER_FIELD, parser=parser, supported=', '.join(SUPPORTED_PROVIDER_NAMES))
        )

    # A non-field error: CSV updates drop the form fields a row omits, and before NetBox 4.6.9 (#22683) a model error
    # keyed on a missing form field raised ValueError
    if errors:
        raise ValidationError(errors)
