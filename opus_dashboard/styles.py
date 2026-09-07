from __future__ import annotations

from nicegui import ui


def apply_styles() -> None:
    ui.add_head_html(
        """
        <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
        <style>
          :root {
            color-scheme: light;
            --app-bg: #f4f7fb;
            --surface: #ffffff;
            --surface-soft: #edf2f8;
            --navy: #1c2545;
            --text: #24304f;
            --muted: #64748b;
            --border: #d6deea;
            --orange: #e04403;
            --teal: #007d6d;
            --red: #b91c1c;
            --amber: #a63a12;
            --sidebar: #1c2545;
          }
          html, body { min-height: 100%; }
          body {
            margin: 0;
            color: var(--text);
            background:
              radial-gradient(circle at 16% 7%, rgba(224,68,3,.075), transparent 23%),
              radial-gradient(circle at 88% 2%, rgba(0,125,109,.07), transparent 21%),
              linear-gradient(145deg, #f9fbfd 0%, #eef4fb 52%, #f4f7fb 100%);
            font-family: Inter, "Segoe UI", Arial, sans-serif;
            font-size: 14px;
          }
          .nicegui-content { padding: 0 !important; }
          .mono { font-family: "Cascadia Mono", "IBM Plex Mono", monospace; }
          .app-shell {
            min-height: 100vh;
            width: 100%;
            display: flex;
            flex-wrap: nowrap !important;
            align-items: stretch;
          }
          .sidebar {
            width: 278px;
            min-width: 278px;
            height: 100vh;
            position: sticky;
            top: 0;
            align-self: flex-start;
            overflow: hidden;
            z-index: 6;
            color: #fff;
            background:
              linear-gradient(180deg, rgba(255,255,255,.035), transparent 22%),
              var(--sidebar);
            border-right: 1px solid rgba(255,255,255,.10);
            box-shadow: 16px 0 38px rgba(28,37,69,.16);
            transition: width 180ms ease, min-width 180ms ease;
          }
          .sidebar.collapsed { width: 86px; min-width: 86px; }
          .sidebar-stack {
            min-height: 100vh;
            height: 100vh;
            box-sizing: border-box;
            padding: 12px;
            gap: 16px;
          }
          .sidebar-brand {
            color: #cbd5e1;
            letter-spacing: .085em;
          }
          .logo-plate {
            min-height: 98px;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 12px;
            overflow: hidden;
            background: #fff;
            border: 1px solid rgba(255,255,255,.78);
            border-radius: 14px;
            box-shadow: 0 12px 28px rgba(0,0,0,.16);
          }
          .brand-logo {
            width: 232px;
            max-width: 100%;
            height: 78px;
            object-fit: contain;
          }
          .brand-logo .q-img__image {
            background-size: contain !important;
            object-fit: contain !important;
          }
          .compact-brand-logo { display: none; }
          .sidebar.collapsed .logo-plate {
            min-height: 62px;
            width: 62px;
            align-self: center;
            padding: 6px;
            border-radius: 12px;
          }
          .sidebar.collapsed .brand-logo { width: 48px; height: 48px; }
          .sidebar.collapsed .full-brand-logo { display: none; }
          .sidebar.collapsed .compact-brand-logo { display: block; }
          .connection-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 14px;
          }
          .sidebar.collapsed .sidebar-label,
          .sidebar.collapsed .sidebar-meta,
          .sidebar.collapsed .sidebar-brand { display: none !important; }
          .nav-btn {
            width: 100%;
            min-height: 44px;
            margin: 2px 0;
            padding: 10px 14px;
            border-radius: 8px;
            color: #fff !important;
            font-weight: 800;
            justify-content: flex-start !important;
          }
          .nav-btn .q-btn__content {
            width: 100%;
            justify-content: flex-start !important;
            gap: 12px;
          }
          .nav-btn .q-icon, .nav-btn .sidebar-label { color: #fff !important; }
          .nav-btn.active {
            background: rgba(224,68,3,.22) !important;
            box-shadow: inset 3px 0 0 #ff7a3d;
          }
          .nav-btn:hover { background: rgba(255,255,255,.09) !important; }
          .sidebar.collapsed .nav-btn {
            width: 48px;
            min-width: 48px;
            height: 48px;
            min-height: 48px;
            align-self: center;
            padding: 0;
            justify-content: center !important;
          }
          .sidebar.collapsed .nav-btn .q-btn__content {
            justify-content: center !important;
            gap: 0;
          }
          .sidebar-panel {
            color: #fff;
            background: rgba(255,255,255,.07);
            border: 1px solid rgba(255,255,255,.14);
            border-radius: 10px;
          }
          .sidebar-muted { color: #bac5dc !important; }
          .sidebar-online { color: #66e3c7 !important; }
          .sidebar-offline { color: #ffb4a2 !important; }
          .sidebar-collapse, .sidebar-collapse * { color: #fff !important; }
          .sidebar-bottom { margin-top: auto; }
          .app-main {
            min-width: 0;
            width: 0;
            flex: 1 1 0;
            position: relative;
            overflow: hidden;
          }
          .toolbar {
            min-height: 76px;
            padding: 14px 24px;
            position: sticky;
            top: 0;
            z-index: 5;
            background: rgba(255,255,255,.84);
            border-bottom: 1px solid rgba(100,116,139,.16);
            backdrop-filter: blur(16px);
          }
          .toolbar-title {
            color: var(--navy);
            font-size: 1.18rem;
            font-weight: 900;
            line-height: 1.25;
          }
          .toolbar-subtitle { color: var(--muted); font-size: .78rem; }
          .content-wrap {
            width: 100%;
            padding: 18px 24px 34px;
            gap: 16px;
            box-sizing: border-box;
          }
          .app-card, .metric-card {
            color: var(--text);
            background: linear-gradient(180deg, rgba(255,255,255,.99), rgba(248,250,253,.99));
            border: 1px solid var(--border);
            border-radius: 10px;
            box-shadow: 0 16px 38px rgba(15,23,42,.075);
          }
          .app-card { padding: 18px; overflow: hidden; }
          .metric-card { min-height: 118px; padding: 16px; }
          .metric-card.clickable {
            cursor: pointer;
            transition: transform 140ms ease, border-color 140ms ease, box-shadow 140ms ease;
          }
          .metric-card.clickable:hover {
            transform: translateY(-2px);
            border-color: rgba(224,68,3,.45);
            box-shadow: 0 18px 42px rgba(15,23,42,.12);
          }
          .metric-card.selected { border: 2px solid var(--orange); }
          .metric-grid {
            display: grid;
            grid-template-columns: repeat(5, minmax(0, 1fr));
            gap: 12px;
          }
          .metric-label {
            color: var(--muted);
            font-size: 11px;
            line-height: 1.25;
            font-weight: 900;
            letter-spacing: .07em;
            text-transform: uppercase;
          }
          .metric-value {
            color: var(--navy);
            font-size: 30px;
            line-height: 1.05;
            font-weight: 900;
          }
          .order-metrics .metric-value {
            font-size: 24px;
            overflow-wrap: anywhere;
          }
          .metric-detail { color: var(--muted); font-size: .78rem; font-weight: 600; }
          .metric-icon {
            color: var(--navy);
            border-radius: 8px;
            padding: 8px;
            background: rgba(28,37,69,.06);
          }
          .section-title {
            color: var(--navy);
            font-size: 1rem;
            line-height: 1.35;
            font-weight: 900;
          }
          .section-subtitle {
            color: var(--muted);
            font-size: .78rem;
            line-height: 1.45;
          }
          .chart-grid {
            display: grid;
            grid-template-columns: minmax(0, 1.5fr) minmax(360px, 1fr);
            gap: 16px;
          }
          .analytics-filter-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(180px, 1fr));
            gap: 12px;
            width: 100%;
          }
          .analytics-chart-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 16px;
            width: 100%;
          }
          .analytics-chart { min-height: 390px; }
          .checklist-total-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(180px, 1fr));
            gap: 10px;
            width: 100%;
          }
          .checklist-total-card {
            padding: 12px 14px;
            border: 1px solid var(--border);
            border-radius: 8px;
            background: var(--surface);
          }
          .chart-frame { width: 100%; height: 350px; }
          .status-banner {
            width: 100%;
            padding: 12px 14px;
            border: 1px solid rgba(0,125,109,.22);
            border-left: 4px solid var(--teal);
            border-radius: 8px;
            color: var(--navy);
            background: rgba(0,125,109,.055);
          }
          .status-banner.warning {
            border-color: rgba(224,68,3,.25);
            border-left-color: var(--orange);
            background: rgba(224,68,3,.055);
          }
          .status-banner.error {
            border-color: rgba(185,28,28,.25);
            border-left-color: var(--red);
            background: rgba(185,28,28,.055);
          }
          .credential-copy { min-width: 0; flex: 1 1 auto; }
          .credential-account {
            max-width: 100%;
            overflow-wrap: anywhere;
            word-break: break-word;
          }
          .process-track {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
            gap: 12px;
            width: 100%;
          }
          .process-node {
            min-height: 128px;
            padding: 14px;
            border: 1px solid var(--border);
            border-top: 3px solid var(--orange);
            border-radius: 9px;
            background: #fff;
          }
          .stage-number {
            width: 30px;
            height: 30px;
            color: #fff;
            background: var(--navy);
            border-radius: 50%;
            font-size: .75rem;
            font-weight: 900;
          }
          .empty-state {
            width: 100%;
            min-height: 170px;
            padding: 28px;
            color: var(--muted);
            border: 1px dashed #b9c5d6;
            border-radius: 9px;
            background: rgba(237,242,248,.62);
          }
          .filter-select { min-width: 190px; }
          .order-filter { width: min(100%, 520px); }
          .checklist-link {
            min-height: 30px;
            padding: 2px 0;
            font-weight: 800;
            text-align: left;
          }
          .checklist-link .q-btn__content {
            justify-content: flex-start;
            text-align: left;
          }
          .checklist-detail-card {
            width: min(1480px, 96vw);
            max-width: 96vw;
            max-height: 92vh;
            padding: 20px;
            gap: 16px;
            overflow-y: auto;
            color: var(--text);
            background: var(--app-bg);
          }
          .checklist-detail-loading { min-height: 260px; }
          .checklist-detail-metrics {
            grid-template-columns: repeat(4, minmax(0, 1fr));
          }
          .primary-action {
            color: var(--navy) !important;
            border: 1px solid rgba(224,68,3,.34);
            border-radius: 8px;
            background: rgba(224,68,3,.10) !important;
            font-weight: 800;
          }
          .secondary-action {
            color: var(--navy) !important;
            border: 1px solid rgba(28,37,69,.16);
            border-radius: 8px;
            background: rgba(255,255,255,.82) !important;
            font-weight: 800;
          }
          .q-field--outlined .q-field__control {
            color: var(--text);
            background: rgba(255,255,255,.86);
          }
          .q-field--outlined .q-field__control:before {
            border-color: var(--border) !important;
          }
          .q-field__native, .q-field__input, .q-field__label,
          .q-select__dropdown-icon { color: var(--text) !important; }
          .q-table__container, .q-table, .q-table thead, .q-table tbody,
          .q-table tr, .q-table th, .q-table td {
            color: var(--text) !important;
            border-color: var(--border) !important;
          }
          .q-table__container {
            overflow: hidden;
            background: var(--surface) !important;
            border: 1px solid var(--border);
            border-radius: 10px;
            box-shadow: 0 8px 22px rgba(15,23,42,.055);
          }
          .q-table thead tr, .q-table thead th {
            color: #fff !important;
            background: var(--navy) !important;
          }
          .q-table thead th {
            min-height: 46px;
            padding: 11px 12px;
            font-size: .71rem;
            font-weight: 800;
            letter-spacing: .035em;
            text-transform: uppercase;
            white-space: normal;
          }
          .q-table thead .q-icon { color: #fff !important; }
          .q-table tbody td { padding: 11px 12px; vertical-align: top; }
          .q-table tbody tr:nth-child(even), .q-table tbody tr:nth-child(even) td {
            background: #f6f8fc !important;
          }
          .q-table tbody tr:hover, .q-table tbody tr:hover td {
            background: #eef3fa !important;
          }
          .q-table__bottom {
            min-height: 48px;
            color: var(--muted);
            background: #f8fafc;
            border-top: 1px solid var(--border);
          }
          .data-table .q-table__middle { overflow-x: auto; }
          .footer-note { color: var(--muted); font-size: .72rem; text-align: center; }
          .fade-in { animation: fade-in 180ms ease-out both; }
          @keyframes fade-in {
            from { opacity: 0; transform: translateY(3px); }
            to { opacity: 1; transform: translateY(0); }
          }
          @media (max-width: 1400px) {
            .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .chart-grid { grid-template-columns: 1fr; }
            .analytics-filter-grid, .checklist-total-grid {
              grid-template-columns: repeat(2, minmax(180px, 1fr));
            }
            .analytics-chart-grid { grid-template-columns: 1fr; }
          }
          @media (max-width: 760px) {
            .connection-grid { grid-template-columns: 1fr; }
            .sidebar { width: 86px; min-width: 86px; }
            .sidebar .sidebar-label, .sidebar .sidebar-meta, .sidebar .sidebar-brand {
              display: none !important;
            }
            .sidebar .logo-plate {
              min-height: 62px;
              width: 62px;
              align-self: center;
              padding: 6px;
            }
            .sidebar .brand-logo { width: 48px; height: 48px; }
            .sidebar .full-brand-logo { display: none; }
            .sidebar .compact-brand-logo { display: block; }
            .sidebar .sidebar-collapse { display: none !important; }
            .sidebar .nav-btn {
              width: 48px;
              min-width: 48px;
              height: 48px;
              padding: 0;
              align-self: center;
            }
            .sidebar .nav-btn .q-btn__content {
              justify-content: center !important;
              gap: 0;
            }
            .toolbar { padding: 12px 16px; }
            .content-wrap { padding: 14px; }
            .metric-grid { grid-template-columns: 1fr; }
            .analytics-filter-grid, .checklist-total-grid {
              grid-template-columns: 1fr;
            }
            .filter-select { min-width: 100%; }
          }
        </style>
        """
    )
