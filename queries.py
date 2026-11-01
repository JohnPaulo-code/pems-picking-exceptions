QUERIES = [
    ("1. Module A detection signal (top locations)",
     """
     SELECT pe.location_id,
            COUNT(DISTINCT pe.event_id) FILTER (
              WHERE EXISTS (SELECT 1 FROM scan_attempts sa
                            WHERE sa.event_id = pe.event_id
                              AND sa.is_accepted = FALSE
                              AND sa.scanned_product_id IS NOT NULL)
            ) AS events_with_rejected_known_wrong
     FROM picking_events pe
     GROUP BY pe.location_id
     ORDER BY events_with_rejected_known_wrong DESC
     LIMIT 8
     """),

    ("2. Hotspot mix (normal still occurs)",
     """
     SELECT l.location_id, l.location_code,
            COUNT(*) FILTER (WHERE x.is_anomaly)     AS abnormal,
            COUNT(*) FILTER (WHERE NOT x.is_anomaly) AS normal
     FROM simulation_event_labels x
     JOIN picking_events pe USING (event_id)
     JOIN locations l USING (location_id)
     WHERE x.run_tag = 'SIM001' AND x.is_hotspot_location = TRUE
     GROUP BY l.location_id, l.location_code
     ORDER BY abnormal DESC
     """),

    ("3. Dominant unexpected product per hotspot",
     """
     SELECT l.location_id, x.unexpected_product_id,
            COUNT(*) AS occurrences,
            COUNT(DISTINCT pe.picker_id) AS distinct_pickers
     FROM simulation_event_labels x
     JOIN picking_events pe USING (event_id)
     JOIN locations l USING (location_id)
     WHERE x.run_tag = 'SIM001' AND x.is_hotspot_location = TRUE
     GROUP BY l.location_id, x.unexpected_product_id
     ORDER BY l.location_id, occurrences DESC
     """),

    ("4. Mixed-item invisibility check",
     """
     SELECT x.scenario,
            COUNT(*) FILTER (WHERE EXISTS (
              SELECT 1 FROM scan_attempts sa
              WHERE sa.event_id = x.event_id AND sa.is_accepted = FALSE
            )) AS events_with_any_rejection,
            COUNT(*) AS total
     FROM simulation_event_labels x
     WHERE x.run_tag = 'SIM001'
     GROUP BY x.scenario
     ORDER BY x.scenario
     """),
]