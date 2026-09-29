-- Registry for installed, content-addressed language packs.
--
-- Identity and installation state are separate tables on purpose. DuckDB rewrites an
-- UPDATE that touches a unique-indexed or foreign-key column as a delete and an insert,
-- which a row referenced by another table refuses; every table that records pack content
-- references the pack, so updating a pack in place would be impossible if version,
-- checksum, and framework lived on the referenced row. `language_packs` is therefore
-- immutable identity and `pack_installations` holds everything an update changes.
--
-- `language_packs` is recreated rather than altered: migration 0005 pinned the maturity
-- domain to values the pack contract does not use, DuckDB cannot drop a CHECK
-- constraint, and a released migration is immutable. The rebuild carries every existing
-- row across and maps the retired maturity values onto the four the contract declares.
-- The carried installation data waits in an unconstrained staging table, because a table
-- cannot be renamed while another table's foreign key depends on it.
CREATE TABLE pack_installations__0008_stage AS
SELECT
    pack_id,
    pack_name,
    version,
    checksum,
    CASE maturity
        WHEN 'mature' THEN 'placement-ready'
        WHEN 'developing' THEN 'pilot'
        ELSE maturity
    END AS maturity,
    framework_id,
    capabilities_json,
    installed_at,
    updated_at
FROM language_packs;

CREATE TABLE language_packs__0008 (
    pack_id      VARCHAR   NOT NULL PRIMARY KEY CHECK (starts_with(pack_id, 'pak_')),
    -- Stable pack identity such as 'pl-pilot'; the display name may change freely.
    pack_key     VARCHAR   NOT NULL UNIQUE CHECK (length(pack_key) > 0),
    language_tag VARCHAR   NOT NULL CHECK (length(language_tag) > 0),
    created_at   TIMESTAMP NOT NULL
);

INSERT INTO language_packs__0008 (pack_id, pack_key, language_tag, created_at)
SELECT pack_id, pack_name, language_tag, installed_at FROM language_packs;

DROP INDEX language_packs_identity;
DROP TABLE language_packs;
ALTER TABLE language_packs__0008 RENAME TO language_packs;

CREATE TABLE pack_installations (
    pack_id            VARCHAR   NOT NULL PRIMARY KEY REFERENCES language_packs (pack_id),
    pack_name          VARCHAR   NOT NULL CHECK (length(pack_name) > 0),
    version            VARCHAR   NOT NULL CHECK (length(version) > 0),
    -- Content address of the whole pack directory; the column keeps its 0005 name.
    checksum           VARCHAR   NOT NULL CHECK (length(checksum) = 64),
    maturity           VARCHAR   NOT NULL CHECK (
        maturity IN ('fixture', 'pilot', 'onboarding-ready', 'placement-ready')
    ),
    framework_id       VARCHAR   REFERENCES proficiency_frameworks (framework_id),
    capabilities_json  VARCHAR   NOT NULL DEFAULT '{}' CHECK (json_valid(capabilities_json)),
    manifest_json      VARCHAR   NOT NULL DEFAULT '{}' CHECK (json_valid(manifest_json)),
    source_policy_json VARCHAR   NOT NULL DEFAULT '{}' CHECK (json_valid(source_policy_json)),
    status             VARCHAR   NOT NULL DEFAULT 'installed' CHECK (
        status IN ('installed', 'superseded')
    ),
    source_path        VARCHAR,
    installed_at       TIMESTAMP NOT NULL,
    updated_at         TIMESTAMP NOT NULL
);

INSERT INTO pack_installations (
    pack_id, pack_name, version, checksum, maturity, framework_id, capabilities_json,
    manifest_json, source_policy_json, status, source_path, installed_at, updated_at
)
SELECT
    pack_id, pack_name, version, checksum, maturity, framework_id, capabilities_json,
    '{}', '{}', 'installed', NULL, installed_at, updated_at
FROM pack_installations__0008_stage;

DROP TABLE pack_installations__0008_stage;

-- Every file the installed pack version covered, so a reinstall can prove it is the
-- same bytes rather than merely the same version string.
CREATE TABLE pack_files (
    pack_id       VARCHAR NOT NULL REFERENCES language_packs (pack_id),
    relative_path VARCHAR NOT NULL CHECK (length(relative_path) > 0),
    sha256        VARCHAR NOT NULL CHECK (length(sha256) = 64),
    byte_count    BIGINT  NOT NULL CHECK (byte_count >= 0),
    PRIMARY KEY (pack_id, relative_path)
);

-- A pack may support several frameworks; a track must select exactly one of them.
CREATE TABLE pack_frameworks (
    pack_id      VARCHAR NOT NULL REFERENCES language_packs (pack_id),
    framework_id VARCHAR NOT NULL REFERENCES proficiency_frameworks (framework_id),
    PRIMARY KEY (pack_id, framework_id)
);
