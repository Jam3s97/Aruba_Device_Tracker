"""Base entity for the IAP control device's switch and number entities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.entity import Entity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import ArubaConfigEntry


class ArubaOptionsEntity(Entity):
    """
    An entity whose state is read from the config entry's options.

    Its state is only written when something calls async_write_ha_state, so
    without this a change made under Configure would leave the entity showing
    the old value. The update listener only redraws the entity; it never
    reloads the entry (see the note in async_setup_entry).
    """

    _entry: ArubaConfigEntry

    async def async_added_to_hass(self) -> None:
        """Redraw whenever the entry's options change."""
        await super().async_added_to_hass()
        self.async_on_remove(self._entry.add_update_listener(self._async_entry_updated))

    async def _async_entry_updated(
        self,
        hass: HomeAssistant,  # noqa: ARG002
        entry: ArubaConfigEntry,  # noqa: ARG002
    ) -> None:
        """Write the current option value to the state machine."""
        self.async_write_ha_state()
