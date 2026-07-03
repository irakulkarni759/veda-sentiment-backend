HOW TO RUN THIS (Mac)
======================

1. Open the "Terminal" app (press Cmd+Space, type "Terminal", hit Enter).

2. In Terminal, navigate to this folder. If you downloaded/unzipped it into
   your Downloads folder, type this and press Enter:

     cd ~/Downloads/reddit-script

   (If it's somewhere else, use that folder's path instead. You can also
   type "cd " with a trailing space, then drag the folder from Finder into
   the Terminal window, then press Enter — it'll fill in the path for you.)

3. Check you have Node.js installed:

     node --version

   If you see something like "v20.11.0", you're good. If you see
   "command not found", install Node.js first: go to https://nodejs.org,
   download the Mac installer (the button that says "LTS"), run it, then
   come back and retry this step.

4. Install the one dependency this script needs:

     npm install

   This will take a few seconds and create a "node_modules" folder.

5. Run the script, filling in your three real values in place of the
   placeholders below (all one command — copy the whole block):

     VEDA_SUPABASE_URL="https://xxxx.supabase.co" \
     VEDA_SUPABASE_SECRET_KEY="your-supabase-service-key" \
     ANTHROPIC_API_KEY="your-anthropic-key" \
     node refresh-reddit-quotes.mjs

   These are the SAME values already sitting in your Lovable env vars
   (Project Settings -> Environment Variables) — just copy them from there.

6. Watch it work. It'll print one line per trend as it goes, e.g.:

     [3/42] Rosemary Oil for Hair Growth ... 2 real quote(s) found

   When it's done, refresh your live Veda site — the results are already
   in your database, no redeploy needed.

If something goes wrong, copy the error message and send it back and I'll
help figure out what happened.
