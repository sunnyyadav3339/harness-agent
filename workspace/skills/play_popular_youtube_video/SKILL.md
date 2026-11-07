---
name: play_popular_youtube_video
description: Opens YouTube, searches for a specified popular music video, and plays the top result.
triggers: ["play a music video on youtube", "watch a popular song on youtube", "find and play a hit song", "play despacito on youtube"]
draft: true
---
# Play Popular YouTube Video

## Preconditions
- Browser is available and capable of navigating to external sites.
- Internet connection is active.
- Target video title or artist is known (e.g., "Despacito").

## Steps
1. **Navigate to YouTube**
   - Action: `browser.open({"url": "https://www.youtube.com"})`
   - Verify: The page loads with HTTP 200 and the YouTube homepage is visible.
   - *Note: If the first attempt times out, retry once before proceeding.*

2. **Search for the Video**
   - Action: `browser.type({"id": "<search_bar_id>", "text": "<video_title>", "enter": true})`
   - Verify: The search results page loads, displaying a list of videos matching the query.
   - *Note: Use the stable ID for the search input field (often `id="2"` or similar in accessibility trees).*

3. **Select the Top Result**
   - Action: `browser.click({"id": "<top_result_id>"})`
   - Verify: The video player loads and the video begins playing. The address bar should update to include `/watch?v=`.
   - *Note: Identify the top result by view count or official channel badge if available. If the click times out, check if the video has already started loading/playing in the background.*

4. **Confirm Playback**
   - Action: `task.done({"summary": "Successfully played '<video_title>' on YouTube"})`
   - Verify: The video is actively playing in the browser.

## Recovery Playbook
- **Navigation Timeout**: If `browser.open` times out, wait 5 seconds and retry the same URL. If it fails again, check network connectivity or try a different browser context.
- **Click Timeout on Video**: If clicking the video result times out, take a screenshot to verify if the video has already started playing. If it is playing, proceed to Step 4. If not, try clicking the video thumbnail or title link directly.
- **Search Bar Not Found**: If the search bar ID is not recognized, use a selector like `input[aria-label="Search"]` or locate the search icon and click it to reveal the input field.

## Learned Heuristics (auto-updated)
- YouTube search bar often has a stable accessibility ID (e.g., `id="2"`) in many browser automation contexts.
- Top search results for popular songs are usually the official music video with the highest view count.
- Browser actions may time out even if the underlying action (like video loading) succeeds; always verify state via screenshot or URL before declaring failure.
