-- Stemmy CLI - Database schema reference
-- Full schema from SQLite (compatible with Turso/libSQL)
-- See docs/DATABASE.md for setup and usage

CREATE TABLE formats ("id" TEXT, "identifier" TEXT, "title" TEXT, "description" TEXT, "theme" TEXT, "source_type" TEXT, "source_url" TEXT, "created_at" TEXT, "updated_at" TEXT, "user_id" TEXT, "image_url" TEXT, "author" TEXT, "language" TEXT, "category" TEXT, "keywords" TEXT, "owner_name" TEXT, "owner_email" TEXT, "copyright" TEXT, "explicit" TEXT, "website_url" TEXT, "feed_type" TEXT, "last_fetched" TEXT, "auto_fetch" TEXT, "is_public" TEXT, "stars_count" TEXT, "shares_count" TEXT, "prompts" TEXT);
CREATE TABLE format_analyses ("id" TEXT, "format_id" TEXT, "created_at" TEXT, "updated_at" TEXT, "status" TEXT, "analysis_types" TEXT, "config" TEXT, "results" TEXT, "error" TEXT, "analyzed_items" TEXT);
CREATE TABLE items ("id" TEXT, "format_id" TEXT, "title" TEXT, "description" TEXT, "source_url" TEXT, "audio_url" TEXT, "duration_seconds" TEXT, "published_at" TEXT, "created_at" TEXT, "updated_at" TEXT, "guid" TEXT, "keywords" TEXT, "episode_number" TEXT, "season_number" TEXT, "explicit" TEXT, "author" TEXT, "image_url" TEXT, "content_html" TEXT, "metadata" TEXT, "user_id" TEXT, "is_public" TEXT, "stars_count" TEXT, "shares_count" TEXT, "transcript_data" TEXT, "transcript_status" TEXT, "assembly_transcript_id" TEXT, "transcript_task_id" TEXT, "fragments_processed" TEXT, "transcript_entities" TEXT, "duration" TEXT);
CREATE TABLE analysis_results ("id" TEXT, "analysis_id" TEXT, "type" TEXT, "metric" TEXT, "value" TEXT, "item_id" TEXT, "created_at" TEXT);
CREATE TABLE characters ("id" TEXT, "name" TEXT, "shortname" TEXT, "description" TEXT, "tags" TEXT, "speaking_style" TEXT, "avatar_url" TEXT, "language" TEXT, "nationality" TEXT, "example_fragments" TEXT, "created_at" TEXT, "updated_at" TEXT, "metadata" TEXT, "prompts" TEXT);
CREATE TABLE character_formats ("character_id" TEXT, "format_id" TEXT, "role" TEXT, "created_at" TEXT);
CREATE TABLE voices ("id" TEXT, "name" TEXT, "voice_id" TEXT, "parent_voice_id" TEXT, "type" TEXT, "description" TEXT, "tags" TEXT, "sample_url" TEXT, "created_at" TEXT, "updated_at" TEXT, "user_id" TEXT, "elevenlabs_id" TEXT, "category" TEXT, "emotion" TEXT, "language" TEXT, "settings" TEXT, "metadata" TEXT, "cors_headers" TEXT, "avatar_url" TEXT, "stars_count" TEXT);
CREATE TABLE character_items ("character_id" TEXT, "item_id" TEXT, "role" TEXT, "voice_id" TEXT, "created_at" TEXT);
CREATE TABLE profiles ("id" TEXT, "updated_at" TEXT, "username" TEXT, "full_name" TEXT, "avatar_url" TEXT, "website" TEXT, "is_admin" TEXT, "role" TEXT, "features" TEXT);
CREATE TABLE stemmies ("id" TEXT, "title" TEXT, "audio_url" TEXT, "created_at" TEXT, "updated_at" TEXT, "user_id" TEXT, "format" TEXT, "duration" TEXT, "is_public" TEXT, "video_url" TEXT, "profile_id" TEXT, "transcript" TEXT, "stars_count" TEXT, "shares_count" TEXT);
CREATE TABLE character_stemmies ("character_id" TEXT, "stemmy_id" TEXT, "role" TEXT, "voice_id" TEXT, "created_at" TEXT);
CREATE TABLE character_voices ("character_id" TEXT, "voice_id" TEXT, "emotion" TEXT, "is_default" TEXT, "created_at" TEXT);
CREATE TABLE entities ("id" TEXT, "text" TEXT, "entity_type" TEXT, "start_time_seconds" TEXT, "end_time_seconds" TEXT, "item_id" TEXT, "format_id" TEXT, "user_id" TEXT, "source_audio_url" TEXT, "fragment_audio_url" TEXT, "transformed_audio_url" TEXT, "confidence" TEXT, "metadata" TEXT, "created_at" TEXT, "updated_at" TEXT, "start_time" TEXT, "end_time" TEXT);
CREATE TABLE entity_patterns ("pattern_id" TEXT, "pattern_template" TEXT, "created_at" TEXT, "updated_at" TEXT);
CREATE TABLE segments ("id" TEXT, "item_id" TEXT, "start_time" TEXT, "end_time" TEXT, "audio_url" TEXT, "created_at" TEXT);
CREATE TABLE fragments ("id" TEXT, "segment_id" TEXT, "voice_id" TEXT, "text" TEXT, "start_time_seconds" TEXT, "end_time_seconds" TEXT, "item_audio_url" TEXT, "order_index" TEXT, "parent_id" TEXT, "created_at" TEXT, "updated_at" TEXT, "confidence" TEXT, "words" TEXT, "type" TEXT, "source_type" TEXT, "speaker_label" TEXT, "metadata" TEXT, "duration" TEXT, "transformed_audio_url" TEXT, "background_audio_url" TEXT, "settings" TEXT, "item_id" TEXT, "stemmy_id" TEXT, "user_id" TEXT, "import_source" TEXT, "fragment_audio_url" TEXT, "start_time" TEXT, "end_time" TEXT);
CREATE TABLE fragments_backup_audio_url ("id" TEXT, "segment_id" TEXT, "voice_id" TEXT, "text" TEXT, "start_time" TEXT, "end_time" TEXT, "audio_url" TEXT, "order_index" TEXT, "parent_id" TEXT, "created_at" TEXT, "updated_at" TEXT, "confidence" TEXT, "words" TEXT, "type" TEXT, "source_type" TEXT, "speaker_label" TEXT, "metadata" TEXT, "duration" TEXT, "transformed_url" TEXT, "background_audio_url" TEXT, "settings" TEXT, "item_id" TEXT, "stemmy_id" TEXT, "user_id" TEXT, "import_source" TEXT, "fragment_audio_url" TEXT);
CREATE TABLE item_stemmy_relations ("item_id" TEXT, "stemmy_id" TEXT);
CREATE TABLE migrations ("name" TEXT, "executed_at" TEXT);
CREATE TABLE shares ("id" TEXT, "user_id" TEXT, "target_type" TEXT, "target_id" TEXT, "share_type" TEXT, "created_at" TEXT);
CREATE TABLE sounds ("id" TEXT, "sound_name" TEXT, "sound_audio_url" TEXT, "description" TEXT, "duration_seconds" TEXT, "waveform_data" TEXT, "is_public" TEXT, "fragment_id" TEXT, "user_id" TEXT, "stars_count" TEXT, "shares_count" TEXT, "metadata" TEXT, "created_at" TEXT, "updated_at" TEXT, "file_size" TEXT, "duration" TEXT);
CREATE TABLE sound_favorites ("sound_id" TEXT, "user_id" TEXT, "created_at" TEXT);
CREATE TABLE sound_format_relations ("sound_id" TEXT, "format_id" TEXT);
CREATE TABLE sound_status_relations ("sound_id" TEXT, "status" TEXT, "assigned_by" TEXT, "assigned_at" TEXT);
CREATE TABLE sound_stemmy_relations ("sound_id" TEXT, "stemmy_id" TEXT, "usage_type" TEXT, "used_at" TEXT);
CREATE TABLE sound_tags ("id" TEXT, "name" TEXT, "created_at" TEXT);
CREATE TABLE sound_tag_relations ("sound_id" TEXT, "tag_id" TEXT);
CREATE TABLE sound_types ("id" TEXT, "name" TEXT, "description" TEXT, "created_at" TEXT);
CREATE TABLE sound_type_relations ("sound_id" TEXT, "type_id" TEXT);
CREATE TABLE stars ("id" TEXT, "user_id" TEXT, "target_type" TEXT, "target_id" TEXT, "created_at" TEXT);
CREATE TABLE stemmy_feedbacks ("id" TEXT, "stemmy_id" TEXT, "transcript" TEXT, "audio_url" TEXT, "character" TEXT, "language" TEXT, "accent" TEXT, "is_positive" TEXT, "focus_area" TEXT, "created_at" TEXT, "updated_at" TEXT);
CREATE TABLE users ("id" TEXT, "features" TEXT, "created_at" TEXT, "updated_at" TEXT);
CREATE TABLE voice_mappings ("id" TEXT, "item_id" TEXT, "voice_id" TEXT, "speaker_label" TEXT, "settings" TEXT, "created_at" TEXT, "updated_at" TEXT);
CREATE TABLE fragment_embeddings (
    fragment_id TEXT PRIMARY KEY,
    embedding BLOB,
    dimensions INTEGER,
    model TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
