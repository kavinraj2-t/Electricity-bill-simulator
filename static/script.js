function updateMachineIndices() {
    document.querySelectorAll("[data-machine-card]").forEach((card, index) => {
        const chip = card.querySelector(".chip");
        if (chip) {
            chip.textContent = `Machine ${String(index + 1).padStart(2, "0")}`;
        }
    });
}

function populateMachineOptions(card) {
    const machineSelect = card.querySelector(".machine-select");
    if (machineSelect.options.length > 0) {
        return;
    }

    Object.keys(window.machineLibrary || {}).forEach((name) => {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        machineSelect.appendChild(option);
    });
}

function syncMachineDefaults(card) {
    const machineSelect = card.querySelector(".machine-select");
    const libraryItem = window.machineLibrary?.[machineSelect.value];
    if (!libraryItem) {
        return;
    }

    card.querySelector('.power-input').value = libraryItem.power_kw;
    card.querySelector('.load-type-select').value = libraryItem.load_type;
    card.querySelector('.efficiency-select').value = libraryItem.efficiency_rating;
}

function updateRangeLabel(card) {
    const range = card.querySelector(".exception-range");
    const label = card.querySelector(".range-value");
    label.textContent = `${Number(range.value).toFixed(1)} hrs`;
}

function syncSelectedDays(card) {
    const select = card.querySelector(".selected-days-multiselect");
    const hidden = card.querySelector(".selected-days-input");
    const values = Array.from(select.selectedOptions).map((option) => option.value);
    hidden.value = values.join(",");

    const daysInput = card.querySelector(".exception-days-input");
    if (card.querySelector(".exception-mode-select").value === "specific") {
        daysInput.value = values.length;
    }
}

function updateExceptionUI(card) {
    const toggle = card.querySelector(".exception-toggle");
    const enabledInput = card.querySelector(".exception-enabled-input");
    const fields = card.querySelector(".exception-fields");
    const mode = card.querySelector(".exception-mode-select").value;
    const dayBlock = card.querySelector(".day-selector-block");
    const daySelect = card.querySelector(".selected-days-multiselect");
    const dayCountInput = card.querySelector(".exception-days-input");

    enabledInput.value = toggle.checked ? "1" : "0";
    fields.classList.toggle("is-disabled", !toggle.checked);
    dayBlock.classList.toggle("is-hidden", !(toggle.checked && mode === "specific"));
    daySelect.disabled = !(toggle.checked && mode === "specific");
    dayCountInput.readOnly = toggle.checked && mode === "specific";

    if (!toggle.checked) {
        card.querySelector(".exception-range").value = 0;
        dayCountInput.value = 0;
        Array.from(daySelect.options).forEach((option) => {
            option.selected = false;
        });
        updateRangeLabel(card);
        syncSelectedDays(card);
    } else if (mode === "specific") {
        syncSelectedDays(card);
    }
}

function updateDayLimit() {
    const maxDays = Number(document.querySelector('input[name="days_in_month"]')?.value || 30);
    document.querySelectorAll(".selected-days-multiselect").forEach((select) => {
        Array.from(select.options).forEach((option) => {
            const day = Number(option.value);
            option.disabled = day > maxDays;
            if (option.disabled) {
                option.selected = false;
            }
        });
        const card = select.closest("[data-machine-card]");
        syncSelectedDays(card);
    });
}

function bindMachineCard(card) {
    populateMachineOptions(card);
    if (!card.dataset.bound) {
        syncMachineDefaults(card);
    }
    updateRangeLabel(card);
    updateExceptionUI(card);

    card.querySelector(".machine-select").addEventListener("change", () => syncMachineDefaults(card));
    card.querySelector(".exception-range").addEventListener("input", () => updateRangeLabel(card));
    card.querySelector(".exception-toggle").addEventListener("change", () => updateExceptionUI(card));
    card.querySelector(".exception-mode-select").addEventListener("change", () => updateExceptionUI(card));
    card.querySelector(".selected-days-multiselect").addEventListener("change", () => syncSelectedDays(card));
    card.querySelector(".remove-machine-btn").addEventListener("click", () => {
        const cards = document.querySelectorAll("[data-machine-card]");
        if (cards.length === 1) {
            syncMachineDefaults(cards[0]);
            cards[0].querySelector('input[name="operating_hours[]"]').value = 12;
            cards[0].querySelector('input[name="quantity[]"]').value = 1;
            cards[0].querySelector('input[name="peak_share[]"]').value = 50;
            cards[0].querySelector(".exception-toggle").checked = false;
            updateExceptionUI(cards[0]);
            return;
        }
        card.remove();
        updateMachineIndices();
    });

    card.dataset.bound = "true";
}

function addMachineCard() {
    const template = document.getElementById("machine-template");
    const container = document.getElementById("machine-container");
    const fragment = template.content.cloneNode(true);
    container.appendChild(fragment);
    bindMachineCard(container.lastElementChild);
    updateDayLimit();
    updateMachineIndices();
}

function baseChartOptions() {
    return {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: {
                labels: {
                    color: "#edf6ff"
                }
            }
        },
        scales: {
            x: {
                ticks: { color: "#d2e7f7" },
                grid: { color: "rgba(255,255,255,0.06)" }
            },
            y: {
                ticks: { color: "#d2e7f7" },
                grid: { color: "rgba(255,255,255,0.06)" }
            }
        }
    };
}

function renderResultCharts() {
    if (!window.resultCharts || typeof Chart === "undefined") {
        return;
    }

    const energyCanvas = document.getElementById("energyBreakdownChart");
    const touCanvas = document.getElementById("touChart");

    if (energyCanvas) {
        new Chart(energyCanvas.getContext("2d"), {
            type: "bar",
            data: {
                labels: window.resultCharts.breakdownLabels,
                datasets: [{
                    label: "Units (kWh)",
                    data: window.resultCharts.breakdownValues,
                    backgroundColor: ["#4de2c5", "#ff8f5c", "#8dc8ff", "#f7d56d", "#7de87c", "#f38dff", "#63f1c4", "#ffa777"],
                    borderRadius: 14
                }]
            },
            options: baseChartOptions()
        });
    }

    if (touCanvas) {
        new Chart(touCanvas.getContext("2d"), {
            type: "doughnut",
            data: {
                labels: window.resultCharts.touLabels,
                datasets: [{
                    data: window.resultCharts.touValues,
                    backgroundColor: ["#ff8f5c", "#4de2c5"],
                    borderWidth: 0
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        labels: {
                            color: "#edf6ff"
                        }
                    }
                }
            }
        });
    }
}

function renderOptimizationCharts() {
    if (!window.optimizationCharts || typeof Chart === "undefined") {
        return;
    }

    const costCanvas = document.getElementById("optimizationCostChart");
    const carbonCanvas = document.getElementById("carbonChart");

    if (costCanvas) {
        new Chart(costCanvas.getContext("2d"), {
            type: "bar",
            data: {
                labels: window.optimizationCharts.labels,
                datasets: [{
                    label: "Amount (Rs)",
                    data: window.optimizationCharts.values,
                    backgroundColor: ["#ff8f5c", "#4de2c5", "#8dc8ff"],
                    borderRadius: 14
                }]
            },
            options: baseChartOptions()
        });
    }

    if (carbonCanvas) {
        new Chart(carbonCanvas.getContext("2d"), {
            type: "bar",
            data: {
                labels: window.optimizationCharts.carbonLabels,
                datasets: [{
                    label: "CO2 (kg)",
                    data: window.optimizationCharts.carbonValues,
                    backgroundColor: ["#ff8f5c", "#4de2c5", "#f7d56d"],
                    borderRadius: 14
                }]
            },
            options: baseChartOptions()
        });
    }
}

document.addEventListener("DOMContentLoaded", () => {
    const addButton = document.getElementById("add-machine-btn");
    if (addButton) {
        addButton.addEventListener("click", addMachineCard);
    }

    document.querySelectorAll("[data-machine-card]").forEach((card) => bindMachineCard(card));
    document.querySelector('input[name="days_in_month"]')?.addEventListener("input", updateDayLimit);
    updateDayLimit();
    updateMachineIndices();
    renderResultCharts();
    renderOptimizationCharts();
});
