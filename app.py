import os
import random
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from flask import Flask, redirect, render_template, request, session, url_for

try:
    from pymongo import DESCENDING, MongoClient
except ImportError:  # pragma: no cover
    DESCENDING = -1
    MongoClient = None


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "industrial-energy-platform-secret")


CO2_FACTOR = 0.82
DEFAULT_TARIFF = {
    "peak_rate": 9.75,
    "off_peak_rate": 5.25,
    "demand_charge_rate": 320.0,
    "peak_window_hours": 8,
}
SIMULATION_CACHE = {}

MACHINE_LIBRARY = {
    "CNC Machine": {"power_kw": 18, "load_type": "Constant Load", "efficiency_rating": "Medium"},
    "HVAC System": {"power_kw": 45, "load_type": "Variable Load", "efficiency_rating": "Medium"},
    "Conveyor Belt": {"power_kw": 12, "load_type": "Constant Load", "efficiency_rating": "High"},
    "Air Compressor": {"power_kw": 30, "load_type": "Variable Load", "efficiency_rating": "Low"},
    "Chiller Plant": {"power_kw": 60, "load_type": "Variable Load", "efficiency_rating": "Low"},
    "Packaging Line": {"power_kw": 22, "load_type": "Constant Load", "efficiency_rating": "Medium"},
    "Injection Molding Press": {"power_kw": 38, "load_type": "Constant Load", "efficiency_rating": "Medium"},
    "Boiler Feed Pump": {"power_kw": 16, "load_type": "Variable Load", "efficiency_rating": "High"},
}

EFFICIENCY_LOAD_FACTOR = {"High": 0.92, "Medium": 1.0, "Low": 1.14}
LOAD_VARIATION_RANGE = {"Constant Load": (-0.4, 0.6), "Variable Load": (-1.2, 2.4)}
EFFICIENCY_PRIORITY = {"High": 1.2, "Medium": 1.0, "Low": 0.75}
SHIFTABLE_LOAD_TYPES = {"Variable Load"}


class SimulationRepository:
    def __init__(self):
        self.memory_store = []
        self.collection = None
        mongo_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
        db_name = os.getenv("MONGODB_DB", "industrial_energy_intelligence")
        collection_name = os.getenv("MONGODB_COLLECTION", "simulation_runs")

        if MongoClient is None:
            return

        try:
            client = MongoClient(mongo_uri, serverSelectionTimeoutMS=1500)
            client.admin.command("ping")
            self.collection = client[db_name][collection_name]
        except Exception:
            self.collection = None

    def save_run(self, payload):
        document = deepcopy(payload)
        document["created_at"] = datetime.utcnow()
        if self.collection is not None:
            result = self.collection.insert_one(document)
            return str(result.inserted_id)

        document["_id"] = str(uuid4())
        self.memory_store.append(document)
        self.memory_store = self.memory_store[-25:]
        return document["_id"]

    def list_runs(self, limit=10):
        if self.collection is not None:
            runs = list(self.collection.find().sort("created_at", DESCENDING).limit(limit))
            for item in runs:
                item["_id"] = str(item["_id"])
            return runs

        return sorted(self.memory_store, key=lambda item: item["created_at"], reverse=True)[:limit]


repository = SimulationRepository()


def parse_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp(value, minimum, maximum=None):
    if maximum is not None:
        value = min(value, maximum)
    return max(value, minimum)


def get_current_record():
    return SIMULATION_CACHE.get(session.get("simulation_id"))


def save_current_record(record):
    simulation_id = session.get("simulation_id") or str(uuid4())
    session["simulation_id"] = simulation_id
    SIMULATION_CACHE[simulation_id] = record


def parse_days_list(raw_value, max_days):
    result = []
    for token in str(raw_value or "").split(","):
        day = parse_int(token.strip(), 0)
        if 1 <= day <= max_days and day not in result:
            result.append(day)
    return sorted(result)


def parse_machine_rows(form):
    names = form.getlist("machine_name[]")
    powers = form.getlist("power_kw[]")
    hours = form.getlist("operating_hours[]")
    counts = form.getlist("quantity[]")
    load_types = form.getlist("load_type[]")
    efficiency_ratings = form.getlist("efficiency_rating[]")
    peak_shares = form.getlist("peak_share[]")

    machines = []
    for index, machine_name in enumerate(names):
        if machine_name not in MACHINE_LIBRARY:
            continue

        library_item = MACHINE_LIBRARY[machine_name]
        power_kw = clamp(parse_float(powers[index], library_item["power_kw"]), 0.5, 1000)
        operating_hours = clamp(parse_float(hours[index], 0), 0.5, 24)
        quantity = clamp(parse_int(counts[index], 1), 1, 100)
        peak_share = clamp(parse_float(peak_shares[index], 50), 0, 100)

        machines.append(
            {
                "id": f"machine_{index}",
                "name": machine_name,
                "power_kw": power_kw,
                "operating_hours": operating_hours,
                "quantity": quantity,
                "load_type": load_types[index] or library_item["load_type"],
                "efficiency_rating": efficiency_ratings[index] or library_item["efficiency_rating"],
                "peak_share": peak_share,
            }
        )
    return machines


def parse_exceptions(form, machines, days_in_month):
    enabled = form.getlist("exception_enabled[]")
    extra_hours = form.getlist("exception_extra_hours[]")
    day_counts = form.getlist("exception_days[]")
    modes = form.getlist("exception_mode[]")
    selected_days = form.getlist("exception_selected_days[]")

    exception_map = defaultdict(list)
    for index, machine in enumerate(machines):
        is_enabled = index < len(enabled) and enabled[index] == "1"
        extra = clamp(parse_float(extra_hours[index] if index < len(extra_hours) else 0, 0), 0, 8)
        mode = modes[index] if index < len(modes) else "random"
        specific_days = parse_days_list(selected_days[index] if index < len(selected_days) else "", days_in_month)
        count = clamp(parse_int(day_counts[index] if index < len(day_counts) else 0, 0), 0, days_in_month)

        if not is_enabled or extra <= 0:
            continue

        if mode == "specific":
            count = len(specific_days)
            if not specific_days:
                continue

        exception_map[machine["id"]].append(
            {
                "machine": machine["name"],
                "extra_hours": extra,
                "days": count,
                "mode": mode,
                "selected_days": specific_days,
            }
        )
    return dict(exception_map)


def choose_random_days(days_in_month, count, rng):
    if count <= 0:
        return []
    count = min(days_in_month, count)
    return sorted(rng.sample(range(1, days_in_month + 1), count))


def calculate_bill_from_totals(peak_units, off_peak_units, demand_kw, tariff):
    peak_cost = peak_units * tariff["peak_rate"]
    off_peak_cost = off_peak_units * tariff["off_peak_rate"]
    demand_charge = demand_kw * tariff["demand_charge_rate"]
    total_bill = peak_cost + off_peak_cost + demand_charge
    return {
        "peak_units": round(peak_units, 2),
        "off_peak_units": round(off_peak_units, 2),
        "peak_cost": round(peak_cost, 2),
        "off_peak_cost": round(off_peak_cost, 2),
        "demand_kw": round(demand_kw, 2),
        "demand_charge": round(demand_charge, 2),
        "total_bill": round(total_bill, 2),
    }


def simulate_facility(machines, days_in_month, tariff, exception_map, seed=None):
    rng = random.Random(seed if seed is not None else random.randint(1, 10**9))
    machine_breakdown = []
    daily_totals = []
    exceptional_conditions = []
    total_peak_units = 0.0
    total_off_peak_units = 0.0
    max_daily_demand = 0.0

    for machine in machines:
        variation_min, variation_max = LOAD_VARIATION_RANGE[machine["load_type"]]
        load_factor = EFFICIENCY_LOAD_FACTOR[machine["efficiency_rating"]]
        variation_profile = [round(rng.uniform(variation_min, variation_max), 3) for _ in range(days_in_month)]
        exception_schedule = []
        exception_lookup = defaultdict(float)

        for exception in exception_map.get(machine["id"], []):
            scheduled_days = exception["selected_days"] if exception["mode"] == "specific" else choose_random_days(days_in_month, exception["days"], rng)
            if not scheduled_days:
                continue

            structured_exception = {
                "machine": machine["name"],
                "extra_hours": round(exception["extra_hours"], 2),
                "days": len(scheduled_days),
                "mode": exception["mode"],
                "selected_days": scheduled_days,
            }
            exception_schedule.append(structured_exception)
            exceptional_conditions.append(structured_exception)
            for day in scheduled_days:
                exception_lookup[day] += exception["extra_hours"]

        daily_records = []
        monthly_units = 0.0
        monthly_peak_units = 0.0
        monthly_off_peak_units = 0.0

        for day in range(1, days_in_month + 1):
            adjusted_hours = max(0.5, machine["operating_hours"] + variation_profile[day - 1] + exception_lookup.get(day, 0.0))
            daily_units = machine["power_kw"] * adjusted_hours * machine["quantity"] * load_factor
            peak_share = machine["peak_share"] / 100.0
            peak_units = daily_units * peak_share
            off_peak_units = daily_units - peak_units
            daily_demand_kw = machine["power_kw"] * machine["quantity"] * (1.0 if machine["load_type"] == "Constant Load" else 0.88) * load_factor

            daily_records.append(
                {
                    "day": day,
                    "hours": round(adjusted_hours, 2),
                    "units": round(daily_units, 2),
                    "peak_units": round(peak_units, 2),
                    "off_peak_units": round(off_peak_units, 2),
                    "demand_kw": round(daily_demand_kw, 2),
                }
            )

            monthly_units += daily_units
            monthly_peak_units += peak_units
            monthly_off_peak_units += off_peak_units

        machine_breakdown.append(
            {
                **machine,
                "daily_records": daily_records,
                "monthly_units": round(monthly_units, 2),
                "monthly_peak_units": round(monthly_peak_units, 2),
                "monthly_off_peak_units": round(monthly_off_peak_units, 2),
                "exceptional_schedule": exception_schedule,
                "exception_day_count": sum(item["days"] for item in exception_schedule),
                "variation_profile": variation_profile,
            }
        )

    for day_index in range(days_in_month):
        total_day_units = sum(machine["daily_records"][day_index]["units"] for machine in machine_breakdown)
        total_day_peak_units = sum(machine["daily_records"][day_index]["peak_units"] for machine in machine_breakdown)
        total_day_off_peak_units = sum(machine["daily_records"][day_index]["off_peak_units"] for machine in machine_breakdown)
        total_day_demand = sum(machine["daily_records"][day_index]["demand_kw"] for machine in machine_breakdown)
        daily_totals.append(
            {
                "day": day_index + 1,
                "units": round(total_day_units, 2),
                "peak_units": round(total_day_peak_units, 2),
                "off_peak_units": round(total_day_off_peak_units, 2),
                "demand_kw": round(total_day_demand, 2),
            }
        )
        total_peak_units += total_day_peak_units
        total_off_peak_units += total_day_off_peak_units
        max_daily_demand = max(max_daily_demand, total_day_demand)

    bill_summary = calculate_bill_from_totals(total_peak_units, total_off_peak_units, max_daily_demand, tariff)
    return {
        "days_in_month": days_in_month,
        "tariff": tariff,
        "machine_breakdown": machine_breakdown,
        "daily_totals": daily_totals,
        "exceptional_conditions": exceptional_conditions,
        "total_units": round(total_peak_units + total_off_peak_units, 2),
        "peak_units": round(total_peak_units, 2),
        "off_peak_units": round(total_off_peak_units, 2),
        "max_demand_kw": round(max_daily_demand, 2),
        "bill_summary": bill_summary,
        "seed": seed,
    }


def rebuild_optimized_simulation(base_simulation, adjusted_hours, adjusted_peak_share):
    optimized = deepcopy(base_simulation)
    total_peak_units = 0.0
    total_off_peak_units = 0.0
    max_daily_demand = 0.0
    optimized_daily_totals = []

    for machine in optimized["machine_breakdown"]:
        hours_value = adjusted_hours.get(machine["id"], machine["operating_hours"])
        peak_share = adjusted_peak_share.get(machine["id"], machine["peak_share"])
        load_factor = EFFICIENCY_LOAD_FACTOR[machine["efficiency_rating"]]
        exception_lookup = defaultdict(float)
        for exception in machine["exceptional_schedule"]:
            for day in exception["selected_days"]:
                exception_lookup[day] += exception["extra_hours"]

        optimized_records = []
        optimized_units = 0.0
        optimized_peak_units = 0.0
        optimized_off_peak_units = 0.0

        for day_index, variation in enumerate(machine["variation_profile"], start=1):
            hours = max(0.5, hours_value + variation + exception_lookup.get(day_index, 0.0))
            units = machine["power_kw"] * hours * machine["quantity"] * load_factor
            peak_units = units * (peak_share / 100.0)
            off_peak_units = units - peak_units
            demand_kw = machine["power_kw"] * machine["quantity"] * (1.0 if machine["load_type"] == "Constant Load" else 0.88) * load_factor

            optimized_records.append(
                {
                    "day": day_index,
                    "hours": round(hours, 2),
                    "units": round(units, 2),
                    "peak_units": round(peak_units, 2),
                    "off_peak_units": round(off_peak_units, 2),
                    "demand_kw": round(demand_kw, 2),
                }
            )
            optimized_units += units
            optimized_peak_units += peak_units
            optimized_off_peak_units += off_peak_units

        machine["optimized_operating_hours"] = round(hours_value, 2)
        machine["optimized_peak_share"] = round(peak_share, 2)
        machine["optimized_daily_records"] = optimized_records
        machine["optimized_units"] = round(optimized_units, 2)
        machine["optimized_peak_units"] = round(optimized_peak_units, 2)
        machine["optimized_off_peak_units"] = round(optimized_off_peak_units, 2)

    for day_index in range(base_simulation["days_in_month"]):
        total_day_units = sum(machine["optimized_daily_records"][day_index]["units"] for machine in optimized["machine_breakdown"])
        total_day_peak = sum(machine["optimized_daily_records"][day_index]["peak_units"] for machine in optimized["machine_breakdown"])
        total_day_off_peak = sum(machine["optimized_daily_records"][day_index]["off_peak_units"] for machine in optimized["machine_breakdown"])
        total_day_demand = sum(machine["optimized_daily_records"][day_index]["demand_kw"] for machine in optimized["machine_breakdown"])
        optimized_daily_totals.append(
            {
                "day": day_index + 1,
                "units": round(total_day_units, 2),
                "peak_units": round(total_day_peak, 2),
                "off_peak_units": round(total_day_off_peak, 2),
                "demand_kw": round(total_day_demand, 2),
            }
        )
        total_peak_units += total_day_peak
        total_off_peak_units += total_day_off_peak
        max_daily_demand = max(max_daily_demand, total_day_demand)

    optimized["optimized_daily_totals"] = optimized_daily_totals
    optimized["optimized_total_units"] = round(total_peak_units + total_off_peak_units, 2)
    optimized["optimized_peak_units"] = round(total_peak_units, 2)
    optimized["optimized_off_peak_units"] = round(total_off_peak_units, 2)
    optimized["optimized_max_demand_kw"] = round(max_daily_demand, 2)
    optimized["optimized_bill_summary"] = calculate_bill_from_totals(
        total_peak_units,
        total_off_peak_units,
        max_daily_demand,
        optimized["tariff"],
    )
    return optimized


def compress_adjustments(adjustments):
    result = {}
    for item in adjustments:
        if item["machine"] not in result:
            result[item["machine"]] = item.copy()
        else:
            result[item["machine"]]["to_hours"] = item["to_hours"]
            result[item["machine"]]["to_peak_share"] = item["to_peak_share"]
            result[item["machine"]]["action"] = item["action"]
    return list(result.values())


def optimize_energy_cost(simulation, target_budget, production_mode):
    adjusted_hours = {machine["id"]: machine["operating_hours"] for machine in simulation["machine_breakdown"]}
    adjusted_peak_share = {machine["id"]: machine["peak_share"] for machine in simulation["machine_breakdown"]}
    optimized = rebuild_optimized_simulation(simulation, adjusted_hours, adjusted_peak_share)
    adjustments = []
    iteration = 0

    sorted_machines = sorted(
        simulation["machine_breakdown"],
        key=lambda item: (
            EFFICIENCY_PRIORITY[item["efficiency_rating"]],
            0 if item["load_type"] in SHIFTABLE_LOAD_TYPES else 1,
            -item["monthly_units"],
        ),
    )

    while optimized["optimized_bill_summary"]["total_bill"] > target_budget and iteration < 1000:
        iteration += 1
        changed = False

        for machine in sorted_machines:
            machine_id = machine["id"]
            current_hours = adjusted_hours[machine_id]
            current_peak_share = adjusted_peak_share[machine_id]

            if machine["load_type"] in SHIFTABLE_LOAD_TYPES and current_peak_share > 10:
                next_peak_share = max(10, current_peak_share - (10 if production_mode != "high_output" else 5))
                adjusted_peak_share[machine_id] = next_peak_share
                optimized = rebuild_optimized_simulation(simulation, adjusted_hours, adjusted_peak_share)
                adjustments.append(
                    {
                        "machine": machine["name"],
                        "action": "shift_to_off_peak",
                        "from_hours": round(current_hours, 2),
                        "to_hours": round(current_hours, 2),
                        "from_peak_share": round(current_peak_share, 2),
                        "to_peak_share": round(next_peak_share, 2),
                    }
                )
                changed = True
            elif current_hours > 2:
                step = 1.0 if production_mode == "efficiency_first" else 0.5
                next_hours = max(2.0, round(current_hours - step, 2))
                if next_hours != current_hours:
                    adjusted_hours[machine_id] = next_hours
                    optimized = rebuild_optimized_simulation(simulation, adjusted_hours, adjusted_peak_share)
                    adjustments.append(
                        {
                            "machine": machine["name"],
                            "action": "reduce_runtime",
                            "from_hours": round(current_hours, 2),
                            "to_hours": round(next_hours, 2),
                            "from_peak_share": round(current_peak_share, 2),
                            "to_peak_share": round(current_peak_share, 2),
                        }
                    )
                    changed = True

            if optimized["optimized_bill_summary"]["total_bill"] <= target_budget:
                break

        if not changed:
            break

    optimized["target_budget"] = target_budget
    optimized["production_mode"] = production_mode
    optimized["budget_met"] = optimized["optimized_bill_summary"]["total_bill"] <= target_budget
    optimized["adjustments"] = compress_adjustments(adjustments)
    return optimized


def build_energy_breakdown(simulation, optimized=None):
    total_units = optimized["optimized_total_units"] if optimized else simulation["total_units"]
    units_key = "optimized_units" if optimized else "monthly_units"
    items = []
    for machine in (optimized or simulation)["machine_breakdown"]:
        units = round(machine[units_key], 2)
        percentage = round((units / total_units) * 100, 2) if total_units else 0
        items.append({"machine": machine["name"], "units": units, "percentage": percentage})
    return sorted(items, key=lambda item: item["units"], reverse=True)


def build_carbon_analysis(simulation, optimized=None):
    co2_emission = round(simulation["total_units"] * CO2_FACTOR, 2)
    co2_saved = 0.0
    optimized_units = None
    optimized_co2 = None
    if optimized:
        optimized_units = round(optimized["optimized_total_units"], 2)
        optimized_co2 = round(optimized_units * CO2_FACTOR, 2)
        co2_saved = round(co2_emission - optimized_co2, 2)
    return {
        "total_units": round(simulation["total_units"], 2),
        "co2_emission": co2_emission,
        "optimized_units": optimized_units,
        "optimized_co2_emission": optimized_co2,
        "co2_saved": co2_saved,
    }


def build_optimization_suggestions(simulation):
    suggestions = []
    current_bill = simulation["bill_summary"]["total_bill"]

    for machine in simulation["machine_breakdown"]:
        peak_shift_share = min(machine["peak_share"], 20 if machine["load_type"] in SHIFTABLE_LOAD_TYPES else 0)
        peak_shift_units = machine["monthly_units"] * (peak_shift_share / 100.0)
        shift_savings = peak_shift_units * (simulation["tariff"]["peak_rate"] - simulation["tariff"]["off_peak_rate"])
        if shift_savings > 0:
            suggestions.append(
                {
                    "machine": machine["name"],
                    "action": "shift_to_off_peak",
                    "cost_savings": round(shift_savings, 2),
                    "co2_reduction": round(peak_shift_units * CO2_FACTOR * 0.08, 2),
                    "recommended_reduction_hours": 0,
                }
            )

        reduction_hours = 2 if machine["monthly_units"] >= simulation["total_units"] * 0.18 else 1
        if machine["operating_hours"] - reduction_hours >= 2:
            reduced_units = machine["power_kw"] * reduction_hours * machine["quantity"] * simulation["days_in_month"] * EFFICIENCY_LOAD_FACTOR[machine["efficiency_rating"]]
            updated_bill = max(0, current_bill - (reduced_units * ((simulation["tariff"]["peak_rate"] + simulation["tariff"]["off_peak_rate"]) / 2)))
            savings = current_bill - updated_bill
            suggestions.append(
                {
                    "machine": machine["name"],
                    "action": "reduce_runtime",
                    "cost_savings": round(savings, 2),
                    "co2_reduction": round(reduced_units * CO2_FACTOR, 2),
                    "recommended_reduction_hours": reduction_hours,
                }
            )

    return sorted(suggestions, key=lambda item: item["cost_savings"], reverse=True)


def build_efficiency_analysis(simulation):
    average_rate = simulation["bill_summary"]["total_bill"] / simulation["total_units"] if simulation["total_units"] else 0
    rows = []
    for machine in simulation["machine_breakdown"]:
        if machine["efficiency_rating"] == "Low":
            status = "low"
            upgrade_factor = 0.18
        elif machine["efficiency_rating"] == "Medium":
            status = "medium"
            upgrade_factor = 0.1
        else:
            status = "high"
            upgrade_factor = 0.04

        upgrade_name = f"High-Efficiency {machine['name']}"
        expected_savings = round(machine["monthly_units"] * average_rate * upgrade_factor, 2)
        rows.append(
            {
                "machine": machine["name"],
                "efficiency_status": status,
                "upgrade_recommendation": upgrade_name,
                "expected_savings": expected_savings,
            }
        )
    return rows


def build_predictive_maintenance(simulation):
    insights = []
    for machine in simulation["machine_breakdown"]:
        daily_units = [day["units"] for day in machine["daily_records"]]
        average = sum(daily_units) / len(daily_units) if daily_units else 0
        peak = max(daily_units) if daily_units else 0
        abnormal = peak > average * (1.3 if machine["load_type"] == "Constant Load" else 1.5)
        insights.append(
            {
                "machine": machine["name"],
                "status": "abnormal_usage_detected" if abnormal else "normal_usage",
                "suggestion": "Schedule inspection for load drift and maintenance review" if abnormal else "Usage is within expected industrial operating band",
            }
        )
    return insights


def build_cost_breakdown(simulation, optimized=None):
    current = simulation["bill_summary"]
    result = {
        "peak_cost": current["peak_cost"],
        "off_peak_cost": current["off_peak_cost"],
        "demand_charge": current["demand_charge"],
        "total_bill": current["total_bill"],
        "optimized_peak_cost": None,
        "optimized_off_peak_cost": None,
        "optimized_demand_charge": None,
        "optimized_total_bill": None,
    }
    if optimized:
        optimized_summary = optimized["optimized_bill_summary"]
        result.update(
            {
                "optimized_peak_cost": optimized_summary["peak_cost"],
                "optimized_off_peak_cost": optimized_summary["off_peak_cost"],
                "optimized_demand_charge": optimized_summary["demand_charge"],
                "optimized_total_bill": optimized_summary["total_bill"],
            }
        )
    return result


def build_dashboard(simulation, optimized=None):
    return {
        "energy_breakdown": build_energy_breakdown(simulation),
        "optimized_energy_breakdown": build_energy_breakdown(simulation, optimized) if optimized else [],
        "carbon_analysis": build_carbon_analysis(simulation, optimized),
        "optimization_suggestions": build_optimization_suggestions(simulation),
        "efficiency_analysis": build_efficiency_analysis(simulation),
        "predictive_maintenance": build_predictive_maintenance(simulation),
        "cost_breakdown": build_cost_breakdown(simulation, optimized),
        "exceptional_conditions": simulation["exceptional_conditions"],
    }


def build_run_snapshot(record):
    simulation = record["simulation"]
    optimized = record.get("optimized")
    return {
        "run_id": str(uuid4()),
        "facility_name": record["facility_name"],
        "simulation": {
            "total_units": simulation["total_units"],
            "peak_units": simulation["peak_units"],
            "off_peak_units": simulation["off_peak_units"],
            "max_demand_kw": simulation["max_demand_kw"],
            "bill_summary": simulation["bill_summary"],
            "machine_breakdown": [
                {
                    "name": machine["name"],
                    "power_kw": machine["power_kw"],
                    "operating_hours": machine["operating_hours"],
                    "quantity": machine["quantity"],
                    "load_type": machine["load_type"],
                    "efficiency_rating": machine["efficiency_rating"],
                    "monthly_units": machine["monthly_units"],
                }
                for machine in simulation["machine_breakdown"]
            ],
        },
        "optimized": {
            "total_units": optimized["optimized_total_units"],
            "bill_summary": optimized["optimized_bill_summary"],
            "budget_met": optimized["budget_met"],
            "target_budget": optimized["target_budget"],
            "adjustments": optimized["adjustments"],
        } if optimized else None,
    }


@app.route("/", methods=["GET"])
def index():
    return render_template(
        "index.html",
        machine_library=MACHINE_LIBRARY,
        default_tariff=DEFAULT_TARIFF,
    )


@app.route("/simulate", methods=["POST"])
def simulate():
    facility_name = request.form.get("facility_name", "Industrial Facility").strip() or "Industrial Facility"
    days_in_month = clamp(parse_int(request.form.get("days_in_month"), 30), 1, 31)
    tariff = {
        "peak_rate": clamp(parse_float(request.form.get("peak_rate"), DEFAULT_TARIFF["peak_rate"]), 0.01, 1000),
        "off_peak_rate": clamp(parse_float(request.form.get("off_peak_rate"), DEFAULT_TARIFF["off_peak_rate"]), 0.01, 1000),
        "demand_charge_rate": clamp(parse_float(request.form.get("demand_charge_rate"), DEFAULT_TARIFF["demand_charge_rate"]), 0.01, 10000),
        "peak_window_hours": clamp(parse_int(request.form.get("peak_window_hours"), DEFAULT_TARIFF["peak_window_hours"]), 1, 24),
    }
    machines = parse_machine_rows(request.form)
    if not machines:
        return redirect(url_for("index"))

    exception_map = parse_exceptions(request.form, machines, days_in_month)
    simulation = simulate_facility(machines, days_in_month, tariff, exception_map, seed=random.randint(1, 10**9))
    dashboard = build_dashboard(simulation)

    record = {
        "facility_name": facility_name,
        "simulation": simulation,
        "optimized": None,
        "dashboard": dashboard,
    }
    save_current_record(record)
    repository.save_run(build_run_snapshot(record))

    return render_template(
        "result.html",
        facility_name=facility_name,
        simulation=simulation,
        dashboard=dashboard,
        database_enabled=repository.collection is not None,
    )


@app.route("/optimize", methods=["GET", "POST"])
def optimize():
    record = get_current_record()
    if not record:
        return redirect(url_for("index"))

    facility_name = record["facility_name"]
    simulation = record["simulation"]
    optimized = record.get("optimized")
    dashboard = record.get("dashboard")

    if request.method == "POST":
        target_budget = clamp(parse_float(request.form.get("target_budget"), 0), 0)
        production_mode = request.form.get("production_mode", "balanced")
        optimized = optimize_energy_cost(simulation, target_budget, production_mode)
        dashboard = build_dashboard(simulation, optimized)
        record["optimized"] = optimized
        record["dashboard"] = dashboard
        save_current_record(record)
        repository.save_run(build_run_snapshot(record))

    return render_template(
        "optimize.html",
        facility_name=facility_name,
        simulation=simulation,
        optimized=optimized,
        dashboard=dashboard,
    )


@app.route("/history", methods=["GET"])
def history():
    runs = repository.list_runs(limit=12)
    return render_template("history.html", runs=runs, database_enabled=repository.collection is not None)


@app.route("/report", methods=["GET"])
def report():
    record = get_current_record()
    if not record:
        return redirect(url_for("index"))

    return render_template(
        "report.html",
        facility_name=record["facility_name"],
        simulation=record["simulation"],
        optimized=record.get("optimized"),
        dashboard=record["dashboard"],
    )


if __name__ == "__main__":
    app.run(debug=True)
