-- Frozen schema of a database created at commit 78621d2
-- (78621d257118d3921fb77278b80def852869fa39).
--
-- This is the oldest models.py whose database the current migrations still
-- upgrade cleanly (earlier commits lack playlists.is_auto_generated, which
-- ensure_playlist_columns' backfill reads). tests/test_migrations.py loads
-- it, runs the migrations and checks the result against today's models.
--
-- Generated, not hand-written: that commit's server/app/models.py (read with
-- `git show`, no checkout) compiled for SQLite with
-- sqlalchemy.schema.CreateTable / CreateIndex in metadata.sorted_tables
-- order - the DDL Base.metadata.create_all ran on a fresh database then.
-- Never edit it to make the test pass; add a migration step instead.

CREATE TABLE library_roots (
	id INTEGER NOT NULL,
	path VARCHAR NOT NULL,
	display_name VARCHAR NOT NULL,
	is_shared BOOLEAN NOT NULL,
	created_at DATETIME NOT NULL,
	last_scanned_at DATETIME,
	last_scan_error VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (path)
);

CREATE TABLE tags (
	id INTEGER NOT NULL,
	name VARCHAR NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (name)
);

CREATE TABLE users (
	id INTEGER NOT NULL,
	username VARCHAR NOT NULL,
	password_hash VARCHAR NOT NULL,
	is_admin BOOLEAN NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_users_username ON users (username);

CREATE TABLE devices (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	name VARCHAR NOT NULL,
	device_type VARCHAR NOT NULL,
	connected_at DATETIME NOT NULL,
	last_seen_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE media_items (
	id INTEGER NOT NULL,
	library_root_id INTEGER NOT NULL,
	kind VARCHAR(11) NOT NULL,
	relative_path VARCHAR NOT NULL,
	title VARCHAR NOT NULL,
	artist VARCHAR,
	album VARCHAR,
	genre VARCHAR,
	track_number INTEGER,
	performer VARCHAR,
	description VARCHAR,
	duration_seconds FLOAT,
	file_mtime FLOAT NOT NULL,
	created_at DATETIME NOT NULL,
	musicbrainz_recording_mbid VARCHAR,
	musicbrainz_artist_mbid VARCHAR,
	cover_art_url VARCHAR,
	lyrics_plain VARCHAR,
	lyrics_synced VARCHAR,
	lyrics_fetched_at DATETIME,
	PRIMARY KEY (id),
	UNIQUE (library_root_id, relative_path),
	FOREIGN KEY(library_root_id) REFERENCES library_roots (id)
);

CREATE TABLE playlists (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	name VARCHAR NOT NULL,
	is_auto_generated BOOLEAN NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE sessions (
	token VARCHAR NOT NULL,
	user_id INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	expires_at DATETIME NOT NULL,
	PRIMARY KEY (token),
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE user_library_access (
	user_id INTEGER NOT NULL,
	library_root_id INTEGER NOT NULL,
	PRIMARY KEY (user_id, library_root_id),
	FOREIGN KEY(user_id) REFERENCES users (id),
	FOREIGN KEY(library_root_id) REFERENCES library_roots (id)
);

CREATE TABLE favorites (
	user_id INTEGER NOT NULL,
	media_item_id INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (user_id, media_item_id),
	FOREIGN KEY(user_id) REFERENCES users (id),
	FOREIGN KEY(media_item_id) REFERENCES media_items (id)
);

CREATE TABLE media_item_tags (
	media_item_id INTEGER NOT NULL,
	tag_id INTEGER NOT NULL,
	PRIMARY KEY (media_item_id, tag_id),
	FOREIGN KEY(media_item_id) REFERENCES media_items (id),
	FOREIGN KEY(tag_id) REFERENCES tags (id)
);

CREATE TABLE play_history (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	media_item_id INTEGER NOT NULL,
	played_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id),
	FOREIGN KEY(media_item_id) REFERENCES media_items (id)
);

CREATE TABLE playback_queue_items (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	position INTEGER NOT NULL,
	media_item_id INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id),
	FOREIGN KEY(media_item_id) REFERENCES media_items (id)
);

CREATE TABLE playback_state (
	user_id INTEGER NOT NULL,
	media_item_id INTEGER,
	position_seconds FLOAT NOT NULL,
	is_playing BOOLEAN NOT NULL,
	active_device_id INTEGER,
	queue_index INTEGER NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (user_id),
	FOREIGN KEY(user_id) REFERENCES users (id),
	FOREIGN KEY(media_item_id) REFERENCES media_items (id),
	FOREIGN KEY(active_device_id) REFERENCES devices (id)
);

CREATE TABLE playlist_items (
	id INTEGER NOT NULL,
	playlist_id INTEGER NOT NULL,
	media_item_id INTEGER NOT NULL,
	position INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(playlist_id) REFERENCES playlists (id),
	FOREIGN KEY(media_item_id) REFERENCES media_items (id)
);

CREATE TABLE ratings (
	user_id INTEGER NOT NULL,
	media_item_id INTEGER NOT NULL,
	stars INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (user_id, media_item_id),
	FOREIGN KEY(user_id) REFERENCES users (id),
	FOREIGN KEY(media_item_id) REFERENCES media_items (id)
);
