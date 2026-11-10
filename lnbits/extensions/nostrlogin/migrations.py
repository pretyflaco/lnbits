async def m001_initial(db):
    await db.execute(
        """
        CREATE TABLE nostrlogin.replay (
            event_id TEXT PRIMARY KEY,
            expires_at INTEGER NOT NULL
        );
        """
    )


async def m002_settings(db):
    await db.execute(
        """
        CREATE TABLE nostrlogin.settings (
            id INTEGER PRIMARY KEY,
            relays TEXT NOT NULL,
            allow_auto_user_creation BOOLEAN NOT NULL DEFAULT false,
            sync_profile_pictures BOOLEAN NOT NULL DEFAULT true,
            enable_diagnostic_logging BOOLEAN NOT NULL DEFAULT false
        );
        """
    )


async def m003_signer_app_image(db):
    await db.execute(
        "ALTER TABLE nostrlogin.settings ADD COLUMN signer_app_image TEXT"
    )

