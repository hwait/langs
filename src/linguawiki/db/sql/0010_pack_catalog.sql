-- The catalogued content of an installed pack: descriptors, resource bundles, activity
-- templates, and reviewed external-source recommendations. Every row is a projection of
-- a `content_records` row, so provenance and review state are never duplicated here.
CREATE TABLE proficiency_descriptors (
    descriptor_id VARCHAR   NOT NULL PRIMARY KEY REFERENCES content_records (content_id),
    pack_id       VARCHAR   NOT NULL REFERENCES language_packs (pack_id),
    framework_id  VARCHAR   NOT NULL REFERENCES proficiency_frameworks (framework_id),
    level_code    VARCHAR   NOT NULL CHECK (length(level_code) > 0),
    dimension     VARCHAR   NOT NULL CHECK (length(dimension) > 0),
    locale        VARCHAR   NOT NULL CHECK (length(locale) > 0),
    descriptor    VARCHAR   NOT NULL CHECK (length(descriptor) > 0),
    created_at    TIMESTAMP NOT NULL
);

CREATE TABLE resource_bundles (
    bundle_id         VARCHAR   NOT NULL PRIMARY KEY REFERENCES content_records (content_id),
    pack_id           VARCHAR   NOT NULL REFERENCES language_packs (pack_id),
    bundle_key        VARCHAR   NOT NULL CHECK (length(bundle_key) > 0),
    bundle_type       VARCHAR   NOT NULL CHECK (length(bundle_type) > 0),
    framework_id      VARCHAR   NOT NULL REFERENCES proficiency_frameworks (framework_id),
    level_code        VARCHAR   NOT NULL CHECK (length(level_code) > 0),
    title             VARCHAR   NOT NULL CHECK (length(title) > 0),
    license           VARCHAR   NOT NULL CHECK (length(license) > 0),
    -- Bundle keys this bundle depends on, resolved recursively at preparation time.
    dependencies_json VARCHAR   NOT NULL DEFAULT '[]' CHECK (json_valid(dependencies_json)),
    created_at        TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX resource_bundles_identity ON resource_bundles (pack_id, bundle_key);

CREATE TABLE resource_bundle_items (
    bundle_id VARCHAR NOT NULL REFERENCES resource_bundles (bundle_id),
    sequence  INTEGER NOT NULL CHECK (sequence >= 1),
    item_kind VARCHAR NOT NULL CHECK (
        item_kind IN ('knowledge', 'descriptor', 'assessment_task', 'activity_template',
                      'source_recommendation', 'example')
    ),
    item_ref  VARCHAR NOT NULL CHECK (length(item_ref) > 0),
    PRIMARY KEY (bundle_id, sequence)
);

CREATE TABLE activity_templates (
    template_id    VARCHAR   NOT NULL PRIMARY KEY REFERENCES content_records (content_id),
    pack_id        VARCHAR   NOT NULL REFERENCES language_packs (pack_id),
    mode           VARCHAR   NOT NULL CHECK (length(mode) > 0),
    title          VARCHAR   NOT NULL CHECK (length(title) > 0),
    level_code     VARCHAR   NOT NULL CHECK (length(level_code) > 0),
    minutes        INTEGER   NOT NULL CHECK (minutes > 0),
    structure_json VARCHAR   NOT NULL DEFAULT '{}' CHECK (json_valid(structure_json)),
    created_at     TIMESTAMP NOT NULL
);

-- Reviewed external material a pack recommends. The core never downloads it; a later
-- stage turns an accepted recommendation into a catalogued source.
CREATE TABLE source_recommendations (
    recommendation_id VARCHAR   NOT NULL PRIMARY KEY REFERENCES content_records (content_id),
    pack_id           VARCHAR   NOT NULL REFERENCES language_packs (pack_id),
    modality          VARCHAR   NOT NULL CHECK (length(modality) > 0),
    title             VARCHAR   NOT NULL CHECK (length(title) > 0),
    creator           VARCHAR,
    locator           VARCHAR,
    level_code        VARCHAR   NOT NULL CHECK (length(level_code) > 0),
    license           VARCHAR   NOT NULL CHECK (length(license) > 0),
    rights_status     VARCHAR   NOT NULL CHECK (
        rights_status IN ('unknown', 'personal-use-only', 'cleared', 'restricted')
    ),
    support_language  VARCHAR,
    notes             VARCHAR,
    created_at        TIMESTAMP NOT NULL
);
