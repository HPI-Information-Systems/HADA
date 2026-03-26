SELECT CAST(row_number() OVER () AS INT) AS the_index, m.matnr AS matnr, m.werks AS werks, m.berid AS berid, NULL AS berty,
                  m.plaab AS plaab, m.planr AS planr, NULL AS dat00, m.delkz AS delkz, m.vrfkz AS vrfkz, m.plumi AS plumi,
                  CASE WHEN m.mng01c = 0 OR m.mng01c IS NULL THEN m.mng01 ELSE to_decimal(m.mng01c, 20, 3) END AS mng01, NULL AS mng03,
                  NULL AS perkz, NULL AS baart, NULL AS plart, NULL AS beskz, NULL AS sobes, NULL AS wrk01, NULL AS lgort, NULL AS delnr,
                  NULL AS vpzuo, NULL AS zuvkz, NULL AS vervp, NULL AS knttp, m.sobkz AS sobkz, m.kdauf AS kdauf, m.kdpos AS kdpos,
                  m.pspel AS pspel, NULL AS lifnr, NULL AS arsnr, NULL AS arsps, NULL AS vrpla, NULL AS pbdnr, NULL AS dbskz, NULL AS reslo,
                  m.sgt_scat AS sgt_scat, NULL AS sgt_rcat, NULL AS resb_dumps, NULL AS resb_schgt, NULL AS xt_lblkz, NULL AS xt_emlif, NULL AS xt_insmk,
                  NULL AS xt_itcons, NULL AS xt_statu, NULL AS eban_pstyp, NULL AS eban_estkz, NULL AS xt_no_disp, NULL AS mdpb_plnkz, NULL AS mdpb_oplkz,
                  NULL AS xt_wepos, NULL AS mfrel, NULL AS knttp_db, NULL AS kzvbr_db, NULL AS kzbws_db
           FROM sapqm7.v_pph_stock AS m
           WHERE (m.matnr ='MM_FERT_00000_BN_______________PROLONGED' and m.werks='M101') or (m.matnr = 'MM_FERT_00000_BN_______________PROLONGED' and m.werks='M202') or (m.matnr = 'MM_FERT_00000_BN_______________PROLONGED' and m.werks='M201') or (m.matnr = 'MM_FERT_00000_BN_______________PROLONGED' and m.werks='M102')
              or (m.matnr = 'MM_FERT_00000_BN_______________PROLONGED' and m.werks='M204') or (m.matnr = 'MM_FERT_00000_BN_______________PROLONGED' and m.werks='M103') or (m.matnr = 'MM_FERT_00000_BN_______________PROLONGED' and m.werks='M203') or (m.matnr = 'MM_FERT_00000_BN_______________PROLONGED' and m.werks='M104')
               AND m.mandt ='910'  AND NOT ( m.mng01 = 0  AND ( m.plaab = '20'  OR m.plaab = '22'  OR m.plaab = '24'));


SELECT CAST(row_number() OVER () AS INT) AS the_index, m.matnr AS matnr, m.werks AS werks, m.berid AS berid, NULL AS berty,
                  m.plaab AS plaab, m.planr AS planr, NULL AS dat00, m.delkz AS delkz, m.vrfkz AS vrfkz, m.plumi AS plumi,
                  CASE WHEN m.mng01c = 0 OR m.mng01c IS NULL THEN m.mng01 ELSE to_decimal(m.mng01c, 20, 3) END AS mng01, NULL AS mng03,
                  NULL AS perkz, NULL AS baart, NULL AS plart, NULL AS beskz, NULL AS sobes, NULL AS wrk01, NULL AS lgort, NULL AS delnr,
                  NULL AS vpzuo, NULL AS zuvkz, NULL AS vervp, NULL AS knttp, m.sobkz AS sobkz, m.kdauf AS kdauf, m.kdpos AS kdpos,
                  m.pspel AS pspel, NULL AS lifnr, NULL AS arsnr, NULL AS arsps, NULL AS vrpla, NULL AS pbdnr, NULL AS dbskz, NULL AS reslo,
                  m.sgt_scat AS sgt_scat, NULL AS sgt_rcat, NULL AS resb_dumps, NULL AS resb_schgt, NULL AS xt_lblkz, NULL AS xt_emlif, NULL AS xt_insmk,
                  NULL AS xt_itcons, NULL AS xt_statu, NULL AS eban_pstyp, NULL AS eban_estkz, NULL AS xt_no_disp, NULL AS mdpb_plnkz, NULL AS mdpb_oplkz,
                  NULL AS xt_wepos, NULL AS mfrel, NULL AS knttp_db, NULL AS kzvbr_db, NULL AS kzbws_db
           FROM sapqm7.v_pph_stock AS m
           WHERE (m.matnr, m.werks) IN ( SELECT matnr, werks FROM tempt )
           AND m.mandt ='910'  AND NOT ( m.mng01 = 0  AND ( m.plaab = '20'  OR m.plaab = '22'  OR m.plaab = '24'));
