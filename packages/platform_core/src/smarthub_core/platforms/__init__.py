"""Provider-specific modules.

Each external provider (Smartsheet, HubSpot, Slack, Stripe, Twilio, ...) lives
under ``smarthub_core.platforms.{provider}``. The ``smarthub_core.platforms.shared``
package holds provider-agnostic abstractions: the ``TokenResolver`` interface,
the ``BaseWebhookHandler`` interface, and OAuth refresh helpers.

Workers and gateways consume providers through these abstractions so that
adding a new provider is additive — implement the interface for that provider
under ``platforms/{provider}/``, register it where needed, and the rest of the
platform sees a uniform shape.
"""
