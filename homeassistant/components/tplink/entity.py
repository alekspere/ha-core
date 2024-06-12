"""Common code for tplink."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Coroutine
import logging
from typing import Any, Concatenate, TypedDict, Unpack

from kasa import (
    AuthenticationError,
    Device,
    DeviceType,
    Feature,
    KasaException,
    TimeoutError,
)
from kasa.iot import IotDevice

from homeassistant.components.light import LightEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import legacy_device_id
from .const import DOMAIN, PRIMARY_STATE_ID
from .coordinator import TPLinkDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


# Mapping from upstream category to homeassistant category
FEATURE_CATEGORY_TO_ENTITY_CATEGORY = {
    Feature.Category.Config: EntityCategory.CONFIG,
    Feature.Category.Info: EntityCategory.DIAGNOSTIC,
    Feature.Category.Debug: EntityCategory.DIAGNOSTIC,
}

# Skips creating entities for primary features supported by a specialized platform.
# For example, we do not need a separate "state" switch for light bulbs.
DEVICETYPES_WITH_SPECIALIZED_PLATFORMS = {
    DeviceType.Bulb,
    DeviceType.LightStrip,
    DeviceType.Dimmer,
}


class EntityDescriptionExtras(TypedDict, total=False):
    """Extra kwargs that can be provided to entity descriptions."""

    entity_registry_enabled_default: bool


def async_refresh_after[_T: CoordinatedTPLinkEntity, **_P](
    func: Callable[Concatenate[_T, _P], Awaitable[None]],
) -> Callable[Concatenate[_T, _P], Coroutine[Any, Any, None]]:
    """Define a wrapper to raise HA errors and refresh after."""

    async def _async_wrap(self: _T, *args: _P.args, **kwargs: _P.kwargs) -> None:
        try:
            await func(self, *args, **kwargs)
        except AuthenticationError as ex:
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="device_authentication",
                translation_placeholders={
                    "func": func.__name__,
                    "exc": str(ex),
                },
            ) from ex
        except TimeoutError as ex:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="device_timeout",
                translation_placeholders={
                    "func": func.__name__,
                    "exc": str(ex),
                },
            ) from ex
        except KasaException as ex:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="device_error",
                translation_placeholders={
                    "func": func.__name__,
                    "exc": str(ex),
                },
            ) from ex
        await self.coordinator.async_request_refresh()

    return _async_wrap


class CoordinatedTPLinkEntity(CoordinatorEntity[TPLinkDataUpdateCoordinator], ABC):
    """Common base class for all coordinated tplink entities."""

    _attr_has_entity_name = True
    _device: Device

    def __init__(
        self,
        device: Device,
        coordinator: TPLinkDataUpdateCoordinator,
        *,
        feature: Feature | None = None,
        parent: Device | None = None,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._device: Device = device
        self._feature = feature

        registry_device = device
        name = device.alias
        if parent and parent.device_type != Device.Type.Hub:
            # Check for SensorEntity can be removed when sensor features are implemented
            if not isinstance(self, SensorEntity) and (
                not feature or feature.category == Feature.Category.Primary
            ):
                # Entity will be added to parent if not a hub and no feature parameter
                # (i.e. core platform like Light, Fan) or feature is primary like state
                registry_device = parent
                name = registry_device.alias
                self._attr_name = device.alias
            else:
                # Prefix the device name with the parent name unless it is a hub attached device.
                # Sensible default for child devices like strip plugs or the ks240 where the child
                # alias makes more sense in the context of the parent.
                # i.e. Hall Ceiling Fan & Bedroom Ceiling Fan; Child device aliases will be Ceiling Fan
                # and Dimmer Switch for both so should be distinguished by the parent name.
                name = f"{parent.alias} {device.alias}"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(registry_device.device_id))},
            manufacturer="TP-Link",
            model=registry_device.model,
            name=name,
            sw_version=registry_device.hw_info["sw_ver"],
            hw_version=registry_device.hw_info["hw_ver"],
        )

        if parent is not None and parent != registry_device:
            self._attr_device_info["via_device"] = (DOMAIN, parent.device_id)
        else:
            self._attr_device_info["connections"] = {
                (dr.CONNECTION_NETWORK_MAC, device.mac)
            }

        self._attr_unique_id = self._get_unique_id()
        self._attr_entity_category = self._category_for_feature(feature)

    def _get_unique_id(self) -> str:
        """Return unique ID for the entity."""
        device = self._device
        if self._feature is not None:
            feature_id = self._feature.id
            # Special handling for primary state attribute (backwards compat for the main switch).
            if feature_id == PRIMARY_STATE_ID:
                return legacy_device_id(device)

            return f"{legacy_device_id(device)}_{feature_id}"

        # Light entities were handled historically differently
        if isinstance(self, LightEntity):
            unique_id = device.mac.replace(":", "").upper()
            # For backwards compat with pyHS100
            if device.device_type is DeviceType.Dimmer and isinstance(
                device, IotDevice
            ):
                # Dimmers used to use the switch format since
                # pyHS100 treated them as SmartPlug but the old code
                # created them as lights
                # https://github.com/home-assistant/core/blob/2021.9.7/homeassistant/components/tplink/common.py#L86
                unique_id = legacy_device_id(device)
            return unique_id

        # For legacy sensors, we construct our IDs from the entity description
        if self.entity_description is not None:
            return f"{legacy_device_id(device)}_{self.entity_description.key}"

    def _category_for_feature(self, feature: Feature | None) -> EntityCategory | None:
        """Return entity category for a feature."""
        # Main controls have no category
        if feature is None or feature.category is Feature.Category.Primary:
            return None

        if (
            entity_category := FEATURE_CATEGORY_TO_ENTITY_CATEGORY.get(feature.category)
        ) is None:
            _LOGGER.error(
                "Unhandled category %s, fallback to DIAGNOSTIC", feature.category
            )
            entity_category = EntityCategory.DIAGNOSTIC

        return entity_category

    @abstractmethod
    @callback
    def _async_update_attrs(self) -> None:
        """Platforms implement this to update the entity internals."""
        raise NotImplementedError

    @callback
    def _async_call_update_attrs(self) -> None:
        """Call update_attrs and make entity unavailable on error.

        update_attrs can sometimes fail if a device firmware update breaks the
        downstream library.
        """
        try:
            self._async_update_attrs()
        except Exception as ex:  # noqa: BLE001
            if self._attr_available:
                _LOGGER.warning(
                    "Unable to read data for %s %s: %s",
                    self._device,
                    self.entity_id,
                    ex,
                )
            self._attr_available = False
        else:
            self._attr_available = True

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._async_call_update_attrs()
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success and self._attr_available


def _entities_for_device[_E: CoordinatedTPLinkEntity](
    device: Device,
    coordinator: TPLinkDataUpdateCoordinator,
    *,
    feature_type: Feature.Type,
    entity_class: type[_E],
    parent: Device | None = None,
) -> list[_E]:
    """Return a list of entities to add.

    This filters out unwanted features to avoid creating unnecessary entities
    for device features that are implemented by specialized platforms like light.
    """
    return [
        entity_class(
            device,
            coordinator,
            feature=feat,
            parent=parent,
        )
        for feat in device.features.values()
        if feat.type == feature_type
        and (
            feat.category != Feature.Category.Primary
            or device.device_type not in DEVICETYPES_WITH_SPECIALIZED_PLATFORMS
        )
    ]


def _entities_for_device_and_its_children[_E: CoordinatedTPLinkEntity](
    device: Device,
    coordinator: TPLinkDataUpdateCoordinator,
    *,
    feature_type: Feature.Type,
    entity_class: type[_E],
) -> list[_E]:
    """Create entities for device and its children.

    This is a helper that calls *_entities_for_device* for the device and its children.
    """
    entities: list[_E] = []
    if device.children:
        _LOGGER.debug("Initializing device with %s children", len(device.children))
        for child in device.children:
            entities.extend(
                _entities_for_device(
                    child,
                    coordinator=coordinator,
                    feature_type=feature_type,
                    entity_class=entity_class,
                    parent=device,
                )
            )

    entities.extend(
        _entities_for_device(
            device,
            coordinator=coordinator,
            feature_type=feature_type,
            entity_class=entity_class,
        )
    )

    return entities


def _description_for_feature[_D: EntityDescription](
    desc_cls: type[_D], feature: Feature, **kwargs: Unpack[EntityDescriptionExtras]
) -> _D:
    """Return description object for the given feature.

    This is responsible for setting the common parameters & deciding based on feature id
    which additional parameters are passed.
    """

    # Disable all debug features that are not explicitly enabled.
    if "entity_registry_enabled_default" not in kwargs:
        kwargs["entity_registry_enabled_default"] = (
            feature.category is not Feature.Category.Debug
        )

    return desc_cls(
        key=feature.id,
        translation_key=feature.id,
        name=feature.name,
        **kwargs,
    )
