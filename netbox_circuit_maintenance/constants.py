# Field lengths the notification handler truncates parser output to before saving
MAX_MAINTENANCE_NAME_LENGTH = 100
MAX_NOTE_TITLE_LENGTH = 200
MAX_NOTIFICATION_SENDER_LENGTH = 200
MAX_NOTIFICATION_SUBJECT_LENGTH = 200

# Upper bound on a stored raw notification, whatever the raw_notification_size setting
MAX_NOTIFICATION_TOTAL_LENGTH = 16384

# Names of the Provider custom fields that migration 0009 creates
PROVIDER_EMAILS_FIELD = 'emails_circuit_maintenances'
PROVIDER_PARSER_FIELD = 'provider_parser_circuit_maintenances'
