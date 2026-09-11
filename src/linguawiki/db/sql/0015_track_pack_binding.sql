-- A track is taught by one installed pack. Until now the binding was re-derived on every
-- read by matching language_packs.language_tag against the track's target language, so a
-- second pack for the same language -- a regional variant, or a replacement mid-course --
-- silently re-pointed an existing track at material it was never built from.
--
-- The column has no foreign key: DuckDB's ALTER TABLE cannot add one, and learning_tracks
-- is referenced by a dozen tables, so recreating it is not available either. The relation
-- is checked instead by integrity.ORPHAN_RELATIONS.
ALTER TABLE learning_tracks ADD COLUMN pack_id VARCHAR;
