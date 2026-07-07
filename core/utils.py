# core/utils.py
"""
Utility functions for spec merging and reconciliation in NETAGENT v2.
Provides deepcopy-safe merge algorithms to preserve LangGraph state immutability.
"""

import copy
import yaml
import logging

logger = logging.getLogger(__name__)


def deep_merge_dicts(base: dict, override: dict) -> dict:
    """
    Ritorna un nuovo dizionario unendo base e override in modo ricorsivo.
    Previene in-place mutation clonando profondamente le strutture.
    Se un valore in override è esplicitamente None, viene rimosso dal risultato.
    """
    if not isinstance(base, dict):
        return copy.deepcopy(override)
    if not isinstance(override, dict):
        return copy.deepcopy(base)

    result = copy.deepcopy(base)
    for k, v in override.items():
        if v is None:
            result.pop(k, None)
        elif k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge_dicts(result[k], v)
        elif k in result and isinstance(result[k], list) and isinstance(v, list):
            if v:
                result[k] = copy.deepcopy(v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def merge_list_by_key(base_list: list, patch_list: list, key: str) -> list:
    """
    Merge deterministico e isolato di liste di dizionari basato su chiave.
    Previene in-place mutation clonando profondamente le strutture dati.
    """
    if base_list is None:
        base_list = []
    if patch_list is None:
        patch_list = []
    cloned_base = copy.deepcopy(base_list)
    cloned_patch = copy.deepcopy(patch_list)
    
    base_map = {item[key]: item for item in cloned_base if isinstance(item, dict) and key in item}
    
    for patch_item in cloned_patch:
        if not isinstance(patch_item, dict):
            continue
        item_key = patch_item.get(key)
        if item_key is None:
            continue
            
        if patch_item.get("delete") is True or patch_item.get("state") == "absent":
            base_map.pop(item_key, None)
        elif item_key in base_map:
            merged = deep_merge_dicts(base_map[item_key], patch_item)
            merged.pop("delete", None)
            merged.pop("state", None)
            base_map[item_key] = merged
        else:
            clean = {k: v for k, v in patch_item.items() if k not in ("delete", "state")}
            base_map[item_key] = clean
            
    return list(base_map.values())


def validate_no_device_loss(known_device_names: set[str], new_spec: str, patch_spec: str = "") -> list[str]:
    """
    Verifica che il nuovo spec non abbia eliminato silenziosamente dispositivi noti.
    Ritorna una lista di nomi di dispositivi mancanti.
    Filtra le rimozioni esplicite richiedendo delete: true o state: absent nella patch.
    """
    if not known_device_names:
        return []
    try:
        parsed = yaml.safe_load(new_spec)
        if not isinstance(parsed, dict):
            return []
        new_names = {
            d["name"] for d in parsed.get("devices", [])
            if isinstance(d, dict) and "name" in d
        }
        missing = known_device_names - new_names
        
        if patch_spec:
            try:
                parsed_patch = yaml.safe_load(patch_spec)
                if isinstance(parsed_patch, dict):
                    explicit_deletes = set()
                    for d in parsed_patch.get("devices", []):
                        if isinstance(d, dict) and "name" in d:
                            if d.get("delete") is True or d.get("state") == "absent":
                                explicit_deletes.add(d["name"])
                    missing = {m for m in missing if m not in explicit_deletes}
            except Exception:
                pass
                
        return sorted(missing)
    except Exception:
        return []


def merge_specifications(old_spec: str, new_spec: str) -> str:
    """
    Esegue il merge intelligente di due specifiche YAML.
    """
    if not old_spec:
        return new_spec
    try:
        old_data = yaml.safe_load(old_spec) or {}
        new_data = yaml.safe_load(new_spec) or {}
    except Exception:
        return new_spec

    if not isinstance(old_data, dict):
        return new_spec
    if not isinstance(new_data, dict):
        return old_spec

    merged_data = copy.deepcopy(old_data)
    
    for k, v in new_data.items():
        if k != "devices":
            if k in merged_data and isinstance(merged_data[k], dict) and isinstance(v, dict):
                merged_data[k] = deep_merge_dicts(merged_data[k], v)
            else:
                merged_data[k] = copy.deepcopy(v)

    old_devs = {d["name"]: d for d in old_data.get("devices", []) if "name" in d}
    new_devs = {d["name"]: d for d in new_data.get("devices", []) if "name" in d}

    merged_devs = copy.deepcopy(old_devs)
    for dev_name, new_dev in new_devs.items():
        if new_dev.get("delete") is True or new_dev.get("state") == "absent":
            merged_devs.pop(dev_name, None)
            continue
            
        if dev_name not in merged_devs:
            merged_devs[dev_name] = copy.deepcopy(new_dev)
        else:
            old_dev = merged_devs[dev_name]
            merged_dev = copy.deepcopy(old_dev)
            
            # Merge simple fields and nested dicts
            for field, val in new_dev.items():
                if field in ["interfaces", "static_routes", "dhcp_pools"]:
                    continue  # Gestiti sotto
                
                if val is None:
                    merged_dev.pop(field, None)
                    continue
                
                if field in merged_dev and isinstance(merged_dev[field], dict) and isinstance(val, dict):
                    merged_dev[field] = deep_merge_dicts(merged_dev[field], val)
                else:
                    merged_dev[field] = copy.deepcopy(val)

            # Merge interfaces (list of dicts keyed by name)
            if "interfaces" in new_dev or "interfaces" in old_dev:
                new_ifaces = new_dev.get("interfaces")
                if "interfaces" in new_dev and (new_ifaces is None or new_ifaces == []):
                    merged_dev["interfaces"] = []
                else:
                    merged_dev["interfaces"] = merge_list_by_key(
                        old_dev.get("interfaces", []) or [],
                        new_ifaces or [],
                        key="name",
                    )

            # Merge static_routes (list of dicts keyed by network)
            if "static_routes" in new_dev or "static_routes" in old_dev:
                new_routes = new_dev.get("static_routes")
                if "static_routes" in new_dev and (new_routes is None or new_routes == []):
                    merged_dev["static_routes"] = []
                else:
                    merged_dev["static_routes"] = merge_list_by_key(
                        old_dev.get("static_routes", []) or [],
                        new_routes or [],
                        key="network",
                    )

            # Merge dhcp_pools (list of dicts keyed by name)
            if "dhcp_pools" in new_dev or "dhcp_pools" in old_dev:
                new_pools = new_dev.get("dhcp_pools")
                if "dhcp_pools" in new_dev and (new_pools is None or new_pools == []):
                    merged_dev["dhcp_pools"] = []
                else:
                    merged_dev["dhcp_pools"] = merge_list_by_key(
                        old_dev.get("dhcp_pools", []) or [],
                        new_pools or [],
                        key="name",
                    )

            # Cleanup internal markers on devices if present
            merged_dev.pop("delete", None)
            merged_dev.pop("state", None)
            merged_devs[dev_name] = merged_dev

    merged_data["devices"] = list(merged_devs.values())
    return yaml.dump(merged_data, default_flow_style=False, sort_keys=False, allow_unicode=True)
