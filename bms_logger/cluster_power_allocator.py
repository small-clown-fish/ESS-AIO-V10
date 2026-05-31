from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List


@dataclass(slots=True)
class PcsDispatchTarget:
    name: str
    requested_kw: float
    limited_kw: float
    reason: str = ""


class ClusterPowerAllocator:
    """EMS-level power allocator for one BMS / multiple PCS cluster.

    The BMS limit is treated as the total cluster limit. The allocator then splits
    the final cluster target across online PCS devices. For commissioning, equal
    split is the safe default.
    """

    @staticmethod
    def clamp_total_power(requested_kw: float, allowed_kw: float | None, margin: float = 1.0) -> tuple[float, bool, str]:
        if allowed_kw is None or allowed_kw <= 0:
            return requested_kw, False, "no valid BMS cluster limit"
        allowed = abs(float(allowed_kw)) * max(0.0, float(margin or 1.0))
        final_abs = min(abs(float(requested_kw)), allowed)
        final = final_abs if requested_kw >= 0 else -final_abs
        return final, abs(final) < abs(float(requested_kw)) - 1e-9, f"BMS allowed cluster power={allowed:.3f}kW"

    @staticmethod
    def equal_split(total_kw: float, pcs_names: Iterable[str]) -> Dict[str, float]:
        names = [str(n) for n in pcs_names if str(n).strip()]
        if not names:
            return {}
        per = float(total_kw) / len(names)
        return {name: per for name in names}

    @staticmethod
    def capacity_weighted(total_kw: float, pcs_configs: Dict[str, Dict[str, Any]], pcs_names: Iterable[str]) -> Dict[str, float]:
        names = [str(n) for n in pcs_names if str(n).strip()]
        if not names:
            return {}
        weights: Dict[str, float] = {}
        for name in names:
            cfg = pcs_configs.get(name, {}) if isinstance(pcs_configs, dict) else {}
            rated = cfg.get("rated_kw") or cfg.get("rated_power_kw") or cfg.get("capacity_kw") or 1.0
            try:
                weights[name] = max(0.0, float(rated))
            except Exception:
                weights[name] = 1.0
        total_weight = sum(weights.values())
        if total_weight <= 0:
            return ClusterPowerAllocator.equal_split(total_kw, names)
        return {name: float(total_kw) * weights[name] / total_weight for name in names}


    @staticmethod
    def topology_weighted(
        total_kw: float,
        pcs_configs: Dict[str, Dict[str, Any]],
        pcs_names: Iterable[str],
        bms_allowed_kw: Dict[str, float],
        power_map: Dict[str, Dict[str, float]] | None,
    ) -> Dict[str, float]:
        """Allocate cluster power by BMS-to-PCS topology weights.

        power_map describes which PCS can use which BMS capacity. Example:
        {
            "PCS-1": {"BMS-1": 1.0, "BMS-2": 0.5},
            "PCS-2": {"BMS-2": 0.5, "BMS-3": 1.0}
        }

        Each PCS gets a per-PCS allowed capacity = sum(BMS allowed * weight).
        The requested cluster power is then distributed proportional to those
        capacities and capped by each PCS capacity.
        """
        names: List[str] = []
        for name in pcs_names:
            cfg = pcs_configs.get(name, {}) if isinstance(pcs_configs, dict) else {}
            if cfg and cfg.get("enabled", True) is False:
                continue
            names.append(str(name))
        if not names:
            return {}
        if not isinstance(power_map, dict) or not power_map:
            return ClusterPowerAllocator.allocate(total_kw, pcs_configs, names, "equal_split")

        caps: Dict[str, float] = {}
        for pcs in names:
            raw_weights = power_map.get(pcs, {}) or {}
            cap = 0.0
            if isinstance(raw_weights, dict):
                for bms_name, weight in raw_weights.items():
                    try:
                        w = max(0.0, float(weight))
                    except Exception:
                        w = 0.0
                    try:
                        bms_cap = max(0.0, float(bms_allowed_kw.get(str(bms_name), 0.0)))
                    except Exception:
                        bms_cap = 0.0
                    cap += bms_cap * w
            caps[pcs] = cap

        total_cap = sum(caps.values())
        if total_cap <= 0:
            return {name: 0.0 for name in names}

        requested_abs = abs(float(total_kw))
        sign = 1.0 if float(total_kw) >= 0 else -1.0
        final_abs = min(requested_abs, total_cap)

        allocation: Dict[str, float] = {}
        remaining = final_abs
        remaining_names = set(names)
        # Progressive cap-aware proportional distribution. This avoids assigning
        # more than a PCS's topology capacity when capacities are uneven.
        while remaining_names and remaining > 1e-9:
            cap_sum = sum(caps[n] for n in remaining_names)
            if cap_sum <= 0:
                break
            changed = False
            for n in list(remaining_names):
                share = remaining * caps[n] / cap_sum
                already = abs(allocation.get(n, 0.0))
                room = max(0.0, caps[n] - already)
                take = min(share, room)
                allocation[n] = sign * (already + take)
                if room <= take + 1e-9:
                    remaining_names.remove(n)
                    changed = True
            allocated_abs = sum(abs(v) for v in allocation.values())
            new_remaining = max(0.0, final_abs - allocated_abs)
            if abs(new_remaining - remaining) < 1e-9 and not changed:
                break
            remaining = new_remaining

        for n in names:
            allocation.setdefault(n, 0.0)
        return allocation

    @staticmethod
    def allocate(total_kw: float, pcs_configs: Dict[str, Dict[str, Any]], pcs_names: Iterable[str], mode: str = "equal_split") -> Dict[str, float]:
        mode = (mode or "equal_split").lower()
        enabled_names: List[str] = []
        for name in pcs_names:
            cfg = pcs_configs.get(name, {}) if isinstance(pcs_configs, dict) else {}
            if cfg and cfg.get("enabled", True) is False:
                continue
            enabled_names.append(str(name))
        if mode in {"capacity_weighted", "capacity", "weighted"}:
            return ClusterPowerAllocator.capacity_weighted(total_kw, pcs_configs, enabled_names)
        return ClusterPowerAllocator.equal_split(total_kw, enabled_names)
