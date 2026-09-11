-- Language-pack registry and framework metadata. Content itself arrives in Stage 2.
CREATE TABLE proficiency_frameworks (
    framework_id VARCHAR   NOT NULL PRIMARY KEY CHECK (length(framework_id) > 0),
    name         VARCHAR   NOT NULL CHECK (length(name) > 0),
    version      VARCHAR   NOT NULL CHECK (length(version) > 0),
    source       VARCHAR,
    created_at   TIMESTAMP NOT NULL
);

CREATE TABLE proficiency_framework_levels (
    framework_id VARCHAR NOT NULL REFERENCES proficiency_frameworks (framework_id),
    level_code   VARCHAR NOT NULL CHECK (length(level_code) > 0),
    sequence     INTEGER NOT NULL CHECK (sequence >= 1),
    PRIMARY KEY (framework_id, level_code)
);

CREATE UNIQUE INDEX proficiency_framework_levels_sequence ON proficiency_framework_levels (
    framework_id, sequence
);

CREATE TABLE language_packs (
    pack_id           VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(pack_id, 'pak_')),
    pack_name         VARCHAR   NOT NULL CHECK (length(pack_name) > 0),
    language_tag      VARCHAR   NOT NULL CHECK (length(language_tag) > 0),
    version           VARCHAR   NOT NULL CHECK (length(version) > 0),
    checksum          VARCHAR   NOT NULL CHECK (length(checksum) = 64),
    maturity          VARCHAR   NOT NULL CHECK (
        maturity IN ('pilot', 'developing', 'placement-ready', 'mature')
    ),
    framework_id      VARCHAR   REFERENCES proficiency_frameworks (framework_id),
    capabilities_json VARCHAR   NOT NULL DEFAULT '{}' CHECK (json_valid(capabilities_json)),
    installed_at      TIMESTAMP NOT NULL,
    updated_at        TIMESTAMP NOT NULL
);

CREATE UNIQUE INDEX language_packs_identity ON language_packs (pack_name, version);
