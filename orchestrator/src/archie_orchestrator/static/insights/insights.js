// Usage insights charts. Each <canvas data-chart="ID" data-kind="KIND"> is drawn from the
// JSON in <script type="application/json" id="chart-ID">, using the vendored Chart.js.
// No inline script or style is needed, so the viewer CSP (script-src 'self') holds.
(function () {
    "use strict";

    var PALETTE = ["#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#76b7b2", "#b07aa1", "#9c755f"];

    // The theme's text colour as the browser resolved it (CSS variables may hold
    // light-dark() values, which Chart.js can't parse).
    function textColor() {
        return getComputedStyle(document.body).color || "#444";
    }

    function money(value) {
        return "$" + Number(value).toFixed(2);
    }

    function data(id) {
        var node = document.getElementById("chart-" + id);
        return node ? JSON.parse(node.textContent) : null;
    }

    function base(textColor, gridColor) {
        return {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            interaction: { mode: "index", intersect: false },
            plugins: { legend: { labels: { color: textColor, boxWidth: 12 } } },
            scales: {
                x: { ticks: { color: textColor }, grid: { color: gridColor } },
                y: { ticks: { color: textColor }, grid: { color: gridColor }, beginAtZero: true },
            },
        };
    }

    var builders = {
        // {days: [...], series: [{model, values}]}
        "stacked-bar": function (d, opts) {
            opts.scales.x.stacked = true;
            opts.scales.y.stacked = true;
            opts.scales.y.ticks.callback = money;
            opts.plugins.tooltip = { callbacks: { label: function (c) {
                return c.dataset.label + ": " + money(c.parsed.y);
            } } };
            return {
                type: "bar",
                data: {
                    labels: d.days,
                    datasets: d.series.map(function (s, i) {
                        return { label: s.model, data: s.values,
                                 backgroundColor: PALETTE[i % PALETTE.length] };
                    }),
                },
                options: opts,
            };
        },
        // {labels, values} — cost per turn
        "bar": function (d, opts) {
            opts.plugins.legend.display = false;
            opts.scales.y.ticks.callback = money;
            opts.scales.x.title = { display: true, text: "Turn", color: opts.scales.x.ticks.color };
            return {
                type: "bar",
                data: { labels: d.labels,
                        datasets: [{ label: "Cost", data: d.values, backgroundColor: PALETTE[0] }] },
                options: opts,
            };
        },
        // {labels, values} — cost per model, horizontal
        "hbar": function (d, opts) {
            opts.indexAxis = "y";
            opts.plugins.legend.display = false;
            opts.scales.x.ticks.callback = money;
            opts.scales.x.beginAtZero = true;
            return {
                type: "bar",
                data: { labels: d.labels,
                        datasets: [{ label: "Cost", data: d.values, backgroundColor: PALETTE[0] }] },
                options: opts,
            };
        },
        // {labels, values, turns} — context tokens per root request
        "line": function (d, opts) {
            opts.plugins.legend.display = false;
            opts.scales.x.title = { display: true, text: "Request", color: opts.scales.x.ticks.color };
            opts.plugins.tooltip = { callbacks: { title: function (items) {
                var i = items[0].dataIndex;
                return "Request " + d.labels[i] + " (turn " + d.turns[i] + ")";
            } } };
            return {
                type: "line",
                data: { labels: d.labels,
                        datasets: [{ label: "Context tokens", data: d.values,
                                     borderColor: PALETTE[0], pointRadius: 0, tension: 0.1 }] },
                options: opts,
            };
        },
        // {days, request_errors, turn_errors, interruptions}
        "multi-line": function (d, opts) {
            opts.scales.y.ticks.precision = 0;
            var series = [["Request errors", d.request_errors, PALETTE[3]],
                          ["Turn errors", d.turn_errors, PALETTE[1]],
                          ["Interruptions", d.interruptions, PALETTE[4]]];
            return {
                type: "line",
                data: {
                    labels: d.days,
                    datasets: series.map(function (s) {
                        return { label: s[0], data: s[1], borderColor: s[2],
                                 backgroundColor: s[2], pointRadius: 2, tension: 0.1 };
                    }),
                },
                options: opts,
            };
        },
    };

    function draw() {
        if (typeof Chart === "undefined") {
            return;
        }
        var text = textColor();
        // A fixed translucent grey reads on both light and dark themes.
        var gridColor = "rgba(127, 127, 127, 0.18)";
        document.querySelectorAll("canvas[data-chart]").forEach(function (canvas) {
            var d = data(canvas.dataset.chart);
            var build = builders[canvas.dataset.kind];
            if (!d || !build) {
                return;
            }
            new Chart(canvas, build(d, base(text, gridColor)));
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", draw);
    } else {
        draw();
    }
})();
