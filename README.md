# RipDoctor

Record a vinyl side, split it into tracks, tag them and file them in your music
library.

## What it does

- **Records** a side from a turntable attached to the machine, with a live meter
  and an auto-stop at the run-out.
- **Or takes a file you already have** - a download, or audio captured
  elsewhere - uploaded from the browser and normalised on the way in. Anything
  ffmpeg can read. Everything after that is identical to a rip.
- **Or takes an album that is already cut** - a download bought one file per
  track, which has no boundaries to find. It goes straight to the tagging step,
  so everything in the library is handled the same way.
- **Or takes a tape** - one uncut lossless recording of songs from different
  albums, with a list of what is in it. The songs are filed under their own albums
  and a playlist keeps them in order. See [Tapes](#tapes).
- **Finds the track boundaries** by looking for gaps in the 1-3 kHz band rather
  than at the full-band level, and by checking them against the track durations
  in MusicBrainz.
- **Lets you correct them by ear.** Each boundary can be auditioned as a short
  clip with a tick mixed in at the cut instant, so you hear whether the cut lands
  in the gap or on the music.
- **Cuts, tags and files** the tracks through [beets](https://beets.io), with
  cover art and album-mode ReplayGain.
- **Archives the raw sides** once it has confirmed the tracks reached the
  library.

You drive it from a web page on the machine that holds the library, or from the
command line.

## Why 1-3 kHz

Splitting a side into tracks is usually done by full-band silence detection: pick
a threshold in decibels, call anything quieter a gap, cut there. On most records
this works.

On a sparse pressing it cannot. A real inter-track gap and a quiet passage inside
a song both sit near -43 dB full-band, and no threshold separates them. Tuning it
trades one failure for the other. On one album this produced two tracks 29
seconds too long and 22 seconds too short.

Measured in 1-3 kHz, the same two things separate by about 20 dB. Vinyl's noise -
plinth rumble, arm handling, warp - is bass-heavy, and a quiet pressing has
nothing at all above 8 kHz. The 1-3 kHz band is where music is and noise is not.

| | full band | 1-3 kHz |
|---|---|---|
| music | -26 | -40 |
| arm up, being handled | -36 | -67 |
| silent groove | -64 | -85 |
| dead air, needle up | -89 | -93 |

Full band puts music and arm-rumble 10 dB apart; the band lane puts them 27 dB
apart. Every boundary decision keys off that.

These are measurements from one signal chain, not universal constants.
[docs/method.md](docs/method.md) explains each number and where it came from, and
`ripdoctor measure` compares your chain against them.

## Getting started

```
pip install ripdoctor
ripdoctor doctor      # what is missing, and what to install
```

`doctor` names every tool it needs - `ffmpeg`, `ffprobe`, `flac`, `metaflac`,
and `arecord` to record - with the install command for each. It also reports
whether beets is configured, which matters more than it looks: without a beets
configuration an import still appears to work while fetching no cover art and
computing no ReplayGain.

Then set the capture device and the two directories, and start the interface:

```
ripdoctor devices     # find your capture device
ripdoctor probe       # record 20 seconds and say what arrived
ripdoctor serve       # http://127.0.0.1:8080
```

On first run `serve` generates a login and prints the password once.

**[The wiki](https://github.com/bspeelm/RipDoctor/wiki) is the manual** -
[installing](https://github.com/bspeelm/RipDoctor/wiki/Installation),
[configuring](https://github.com/bspeelm/RipDoctor/wiki/Configuration),
[setting beets up](https://github.com/bspeelm/RipDoctor/wiki/Setting-up-beets),
and each step of a record from the needle drop to the archive.

| | |
|---|---|
| [Ripping a side](https://github.com/bspeelm/RipDoctor/wiki/Ripping-a-side) | recording, the meter, the auto-stop, salvaging an interrupted capture |
| [Uploading a file](https://github.com/bspeelm/RipDoctor/wiki/Uploading-a-file) | bringing in audio that was not recorded here |
| [Pre-cut album import](https://github.com/bspeelm/RipDoctor/wiki/Pre-cut-album-import) | an album bought already split into tracks |
| [Tapes](https://github.com/bspeelm/RipDoctor/wiki/Tapes) | one uncut recording of songs from different albums, from upload to playlist and archive |
| [First pass](https://github.com/bspeelm/RipDoctor/wiki/First-pass) | finding the release and fitting the boundaries |
| [Checking the boundaries](https://github.com/bspeelm/RipDoctor/wiki/Checking-the-boundaries) | the ear check, and moving a cut |
| [Cutting](https://github.com/bspeelm/RipDoctor/wiki/Cutting-tracks) and [importing](https://github.com/bspeelm/RipDoctor/wiki/Importing) | into `review/`, then into the library |
| [Archiving](https://github.com/bspeelm/RipDoctor/wiki/Archiving) | putting the raw sides away once the tracks are confirmed |
| [Re-ripping](https://github.com/bspeelm/RipDoctor/wiki/Re-ripping-a-side) and [punching](https://github.com/bspeelm/RipDoctor/wiki/Punching-a-track) | redoing one side, or one track |
| [Command line](https://github.com/bspeelm/RipDoctor/wiki/Command-line) | all 15 commands and their options |
| [Troubleshooting](https://github.com/bspeelm/RipDoctor/wiki/Troubleshooting) | errors, and what each one means |

## A record's page

**Cut** is one pipeline rail, one waveform and one table of tracks. The name in
brackets after a claim is the test that proves it.

The rail runs Rip, First pass, Check, Cut tracks, Import, Archive
(`test_the_record_s_rail_runs_in_the_order_the_work_does`), each step holding its own
buttons (`test_a_moved_button_keeps_its_id_on_its_step`) and saying what it waits on
(`test_a_catalogue_placed_track_offers_review_and_the_rail_waits_on_it`). The waveform stays in view while the
table scrolls (`test_the_waveform_stays_in_view_while_the_tables_scroll`), and on a phone
the page collapses as a tape's does
(`test_the_record_s_page_fits_a_phone_the_way_a_tape_s_does`).

The **Boundary** column says who settled each track
(`test_the_boundary_column_says_who_settled_a_track`):

| It reads | Which means | Does Cut wait? |
|---|---|---|
| `Fitted to gap` | found on a measured gap (`test_tracks_found_on_gaps_say_so`) | no |
| `Fitted` | a plan that does not say what the fit rested on (`test_a_plan_that_does_not_say_what_a_fit_rested_on_makes_no_claim_about_a_gap`) | no |
| `Placed by length` | no gap near it, so placed from the catalogue's length (`test_a_track_with_no_gap_says_it_was_placed_by_length`) | no (`test_a_track_placed_by_length_does_not_stop_cut`) |
| `From catalogue · check` | on a side the gaps would not fit, placed from the catalogue because you asked | yes (`test_a_track_placed_from_the_catalogue_blocks_the_cut_and_is_named`) |
| `From an earlier capture` | a time on audio since captured again (`test_a_replaced_side_reads_as_from_an_earlier_capture_before_anything_is_run`) | yes (`test_a_cut_of_a_replaced_side_is_refused_by_name_before_anything_is_saved`) |
| `Checked by ear` | you listened and pressed Checked | no |
| `Set by you` | you moved it (`test_every_way_of_moving_a_boundary_marks_it_set_by_the_person`) | no |
| `Overlaps 3` | it shares audio with track 3 (`test_the_row_and_the_rail_say_a_track_overlaps`) | yes |

Checked keeps a boundary where it is and is offered only on a track in doubt
(`test_checked_keeps_the_boundaries_and_only_a_track_in_doubt_offers_it`). What you set or
checked is held by a later first pass (`test_fitting_again_never_moves_what_a_person_settled`),
unless you tick the box that asks otherwise
(`test_starting_over_is_offered_unticked_and_names_how_many`). Tick clips are never refused,
because they are how a doubt gets heard
(`test_tick_clips_are_how_a_doubt_is_heard_so_they_are_not_refused`).

A marker has to be chosen before it moves. Selecting a track arms nothing, because a nudge
sets a boundary for good (`test_selecting_a_track_does_not_arm_a_marker_for_the_arrow_keys`).
Press the marker, its Start or End cell or button, or `E`
(`test_a_marker_is_armed_only_when_it_was_asked_for`), and the readout says which is chosen
(`test_the_readout_says_which_marker_is_chosen`). A move past the neighbouring track does not
happen, and the status line names that track
(`test_a_boundary_that_would_cross_its_neighbour_does_not_move_and_says_why`).

The keys are shared with Tapes (`test_the_keys_are_the_same_on_a_record_and_a_tape`):

| Key | Cut | Tapes |
|---|---|---|
| `A` | hear the boundary | hear the boundary |
| `↵` | Checked | Accept |
| `S` | split the track | nothing |
| `X` | nothing | skip the song |
| `E` | choose the marker, then swap it | the same |
| `J` `K` | next and previous track | next and previous song |
| `N` | next doubt, on either side (`shift` for the one before) | next doubt (`shift` for the one before) |
| `,` `.` | move the chosen marker 0.05 s (`shift` for 0.5 s) | the same |

Edits save by themselves, and there is nothing to press to keep one
(`test_there_is_no_save_to_press_and_nothing_to_be_prompted_about`). A failed save stays on
screen with its reason (`test_every_edit_saves_once_and_a_failure_stays_said`), and a page left
open cannot overwrite a newer plan
(`test_a_save_from_a_page_left_open_is_refused_and_nothing_is_written`). Moving a boundary back
is the undo.

**A side captured again.** Its times are positions on audio that has gone, so its rows read
`From an earlier capture` and Cut refuses them. A first pass lets them go and fits afresh
(`test_a_first_pass_after_a_re_rip_does_not_hold_times_on_the_audio_that_went`), and Checked
after listening clears a row (`test_checked_on_a_row_from_an_earlier_capture_is_a_way_out`).
**Align from archive** carries the old boundaries onto the new capture until you move or check
a boundary on that side, which ends the offer. Editing a title or an album field does not. A
side no longer on the archived capture is not offered Align, and is told what remains
(`test_a_side_saved_over_is_not_offered_align_and_is_told_the_ways_that_remain`).

**When the album is already filed**, the import asks what to do with it, and stays disabled until
you tick that you know (`test_nothing_is_deleted_without_the_acknowledgement`). An album that
arrived cut defaults to Keep both, and Replace there needs a tick too. An import is refused while
that record is recording or encoding.

- **Replace** takes that exact album or release and nothing else: the whole release id, or with
  none the album artist and album
  (`test_a_replace_once_deleted_every_album_whose_title_held_this_one`,
  `test_part_of_a_release_id_once_deleted_an_album_it_was_part_of`). Nothing exact is refused with
  nothing touched (`test_a_replace_of_nothing_exact_is_refused_with_nothing_touched`), and so are
  two releases under one name (`test_a_replace_of_two_releases_under_one_name_is_refused`). The old
  copy is moved aside, and deleted only once the new one is proved: its audio
  (`test_replacing_removes_the_filed_audio_not_only_the_rows`), the replaced album row's own covers,
  and then the folders that leaves empty (`test_replacing_once_left_the_folders_it_had_emptied`). A
  folder holding anything else stays (`test_a_folder_holding_anything_else_stays`).
- **Keep both** leaves the filed copy where it is.
- **Merge** needs a release and is not offered without one
  (`test_merge_is_not_offered_where_beets_has_nothing_to_merge_by`). With every row of the album
  on that release, beets rematches them, which rewrites their tags, and says so
  (`test_merge_once_stopped_at_the_second_apply_and_now_answers_it`). Beside another edition it
  joins that release's own row and leaves the edition alone
  (`test_merge_beside_another_edition_joins_its_row_and_names_no_other`). With only another edition
  held it refuses before anything runs, and Keep both is the way in
  (`test_merge_never_runs_where_another_edition_would_be_rewritten`).

An empty library root with beets rows under it is a library off its mount, so nothing is filed
there, on a record (`test_a_record_is_refused_where_the_library_is_empty_and_beets_says_otherwise`),
an album that arrived cut (`test_an_album_that_arrived_cut_is_refused_the_same_way`) or a tape
(`test_filing_onto_an_empty_mount_point_once_said_filed_and_wrote_a_playlist`). A truly empty
library still files its first album
(`test_a_library_with_nothing_in_it_and_no_rows_is_new_and_not_gone`).

There is no Archive button. Archiving is the last step of an import, or of File and Playlist on a
tape, when every check passes. If it can't complete, the import still stands, the reason is shown,
and only then does **Archive again** appear. A side captured again is not archived until that
capture has been cut and imported
(`test_a_re_rip_the_library_was_not_cut_from_cannot_cost_the_earlier_capture`). Once the new side
reads back, the archived side it replaces is removed
(`test_the_earlier_take_is_gone_once_the_new_one_reads_back`), or kept with `keep_superseded`
(`test_the_earlier_take_is_kept_when_asked_for`).

## The meter

While a capture runs, two bars share one scale, from -100 dB to 0
(`test_the_scale_is_straight_and_ends_where_it_says`). The 1-3 kHz bar is the one to watch, and
the needle goes down when it sits in `silent groove`. The zones, in dB
(`test_the_meter_says_what_the_chain_measures_and_nothing_a_mock_drew`):

| Full band | | 1-3 kHz | |
|---|---|---|---|
| below -68 | dead air / needle up | below -92 | dead air |
| -68 to -60 | groove | -92 to -72 | silent groove |
| -60 to -45 | quiet music | -72 to -58 | arm-up rumble |
| -45 and up | music | -58 to -44 | quiet music |
| | | -44 and up | music |

Beneath them is the 1-3 kHz level over the last 30 seconds, on the same scale and zones
(`test_the_history_forgets_what_has_left_its_window`). A stretch with no readings is left empty
(`test_a_stretch_with_no_readings_is_drawn_as_nothing`), and a reading the capture has stopped
replacing is not shown as a level
(`test_a_reading_the_capture_loop_stopped_replacing_is_not_a_level`).

## Tapes

A tape is one uncut recording of songs that belong to different albums, such as a
mixtape in a single file. Its steps are the same as a record's, and only tagging
and filing work per song. The [Tapes wiki page](https://github.com/bspeelm/RipDoctor/wiki/Tapes)
walks through it.

Its boundaries are checked on the same waveform editor a record's are
(`test_a_tape_s_waveform_is_the_rip_s_editor_and_player_and_not_a_copy_of_them`).
A boundary you drag is yours and nothing moves it afterwards
(`test_a_move_by_hand_is_the_person_s_and_nothing_afterwards_moves_it`). It moves that song
and no other, so audio left between two songs belongs to neither
(`test_dragging_an_end_earlier_laid_the_next_song_onto_it_and_moves_no_other`), and one that
would pass the song beside it is refused and goes back
(`test_a_move_by_hand_past_a_neighbour_or_the_ends_is_refused`). Two markers on one spot can
be pulled apart (`test_markers_on_one_spot_can_be_pulled_apart_by_either_of_them`). The cut waits until
every doubt is moved, accepted or skipped
(`test_accepting_settles_a_doubt_and_the_cut_waits_until_it_is_settled`).

The page is one pipeline rail, one waveform and one table of songs, with a row that
opens (`test_a_tape_s_songs_are_in_one_table`,
`test_the_pipeline_rail_runs_in_order_with_archive_last`). Once the audio and the list are
both in, boundaries and albums follow by themselves
(`test_a_tape_goes_on_by_itself_once_its_audio_and_list_are_both_in`). A run that failed
waits for you, even after a restart
(`test_a_failed_run_waits_for_a_person_even_after_a_restart`), and Cut pressed during an
automatic lookup runs when it ends
(`test_cut_waits_behind_an_automatic_lookup_instead_of_being_turned_off`). A cut needs settled
boundaries, and filing also needs every song's album
(`test_a_cut_is_not_said_to_wait_on_an_album_and_a_file_is`). A row chooses its album from
one line per option, and a choice more than 3 s from the list asks first, in that line
(`test_an_option_is_one_line_of_columns_with_no_edition_jargon`,
`test_a_release_far_from_the_list_is_asked_about_and_sent_again`). A page left open cannot
put back a list that has changed since
(`test_a_stale_page_reverted_a_row_and_is_refused_with_nothing_changed`), and if the server
goes away while a job runs the page says so and carries on when it is back
(`test_with_the_server_gone_the_page_kept_saying_working_and_now_says_so`).

What you do to a row runs beside an automatic lookup, which reads the tape again for every song
(`test_an_edit_during_a_lookup_was_refused_and_lost_and_now_lands`,
`test_a_choice_during_a_lookup_is_not_overwritten_by_the_answer_it_overtook`). A field saves
once, when you leave it (`test_a_redraw_saved_half_typed_text_because_a_removed_input_counts_as_left`),
and what you press on a row follows that song if it moves
(`test_an_edit_behind_a_move_landed_on_the_row_that_arrived_at_its_old_place`). Songs with no
match at all can be filed from the list in one action, which says what it counts
(`test_a_hundred_songs_with_no_match_cost_two_hundred_clicks`). A lost connection is one plain
message that clears itself (`test_nothing_was_said_of_a_lost_connection_until_a_press_gave_failed_to_fetch`).
A copy in the library is only taken as the song on the recording a match chose, and any other that
agrees is yours to confirm (`test_a_live_album_of_the_list_s_length_was_owned_with_nobody_asked`).
A tape with a song filed can still place the rest
(`test_every_song_owned_and_a_copy_gone_at_file_was_a_dead_end`).

A filed song with no cover says so, and one uploaded image is embedded and set as its
album's art (`test_a_filed_song_with_no_cover_says_so_and_one_with_a_cover_does_not`,
`test_one_upload_embeds_and_sets_the_album_s_art_for_all_its_songs`). Only a song this tape
filed can take one (`test_a_song_this_tape_only_found_could_take_a_cover_and_is_refused`), and
what shares its folder keeps its modes
(`test_a_cover_changed_the_mode_of_a_neighbour_s_file_and_touches_only_its_own`). The Playlist step says
the file is written and no more, because whether a music server shows it is that server's
to say (`test_the_playlist_step_claims_the_file_and_not_the_music_server`).

The list is a CSV with one playlist per file
(`test_two_playlists_in_one_file_are_told_to_split`), or is built by hand on the
tape's page, and both produce the same document
(`test_a_csv_and_a_list_by_hand_write_the_same_document`):

```
playlist,owned,position,title,artist,album,duration
Road Trip,no,1,First Song,A Band,A Record,3:45
Road Trip,no,2,"Second Song, Part One",Another Band,Another Record,4:02
```

- Every column has to be there (`test_a_missing_column_is_named`). `owned` is not
  used, because the library itself is asked
  (`test_the_owned_column_changes_nothing`).
- Positions start at 1 with none missing
  (`test_positions_that_do_not_begin_at_one_were_renumbered_and_are_now_refused`,
  `test_a_gap_in_positions_names_what_is_missing`).
- A bad row is refused with its row number and what to change
  (`test_a_bad_list_is_refused_with_its_row_and_changes_nothing`).

What it does with the list:

- **A song in doubt waits for you.** Silences at -60 dBFS are candidates, and the
  list's lengths, in order, decide which are song boundaries
  (`test_gaps_are_taken_at_a_fixed_level_whatever_the_music_is`). A song with a
  silence inside it is always flagged
  (`test_a_song_with_a_silence_inside_is_placed_whole_and_flagged`), a list out of
  order is never reordered
  (`test_a_list_out_of_order_is_a_mismatch_and_is_never_reordered`), and nothing
  is cut while a doubt stands
  (`test_cut_is_refused_while_a_doubt_is_unheard_and_cuts_nothing`).
- **A song already in the library is not filed again.** It shows as
  `already there` and the playlist points at the copy you have
  (`test_a_song_owned_by_recording_id_is_not_imported_and_says_where`,
  `test_a_song_owned_on_another_album_keeps_its_line_through_a_rebuild`). That
  takes evidence: a row with another recording id is another recording
  (`test_a_row_with_another_recording_id_never_owns_a_pinned_song`), and a row
  with the same title, artist and length but no id waits for you to listen
  (`test_a_row_with_no_recording_id_is_for_a_person_to_confirm_on_a_pinned_song`). A song
  the lookup found in the library needs no cut
  (`test_a_song_found_in_the_library_was_cut_anyway_and_needs_no_cut`).
- **A wrong version is held for you.** A word in a title excuses only the version its
  bracket or dash suffix names
  (`test_a_word_in_a_title_excused_a_version_of_it_it_does_not_name`), and a guest clause
  holding a version word is that version
  (`test_a_guest_clause_ate_the_version_and_graded_exact_against_the_plain_title`).
- **A filed song keeps its release**
  (`test_a_song_in_the_library_keeps_the_release_it_was_filed_as`), and a song whose
  filing failed can still be changed
  (`test_a_song_whose_filing_failed_can_still_be_changed`). Finding boundaries again leaves a song
  the library holds where it is
  (`test_find_again_used_to_refuse_a_tape_with_one_song_filed_and_one_to_place`), and a new list
  is refused,
  until that copy is removed with beets
  (`test_a_confirmed_new_list_dropped_the_lock_on_a_filed_song_and_is_refused`,
  `test_a_new_list_after_beets_removes_the_copies_clears_filing_and_files_them_again`).
- **The playlist is an `.m3u8` file** in the library's `Playlists` folder
  (`test_the_file_lands_in_the_library_playlists_folder_as_utf8`), with paths
  relative to it (`test_the_text_is_the_header_then_relative_lines_in_list_order_with_lf`).
- **The uncut file is archived only once every song resolves**
  (`test_archive_is_refused_with_every_reason_while_a_song_is_unresolved`).

A song from an album you hold in another version goes in as its own album entry and
leaves your tracks unchanged
(`test_merging_into_another_edition_once_rewrote_an_owned_album_so_it_keeps_both`), with the
folder it lands in said only as far as your beets paths promise it
(`test_a_note_promised_the_album_s_folder_where_paths_give_it_a_folder_of_its_own`). A later song
of that version joins that entry
(`test_a_later_song_of_a_held_edition_once_made_another_row_and_cover_and_now_joins`), unless your
paths give each entry a folder of its own, when it goes beside
(`test_paths_that_give_each_row_a_folder_go_beside_with_their_own_art`).

Filing a tape merges songs into albums, and beets crashes doing that when a library
row holds a relative path, which an older library can. `ripdoctor doctor` counts
those rows and prints the fix
(`test_library_rows_with_relative_paths_are_counted_with_the_fix`).

## Two things it will not do

Nothing is cut until the plan validates. A track that would end before it
starts, overlap its neighbour, collide on a track number or run past the end of
the side is refused with the reason.

The raw sides are not cleared until the record is provably in the library.
`ripdoctor archive` asks the importer where it filed things and counts what
arrived.

## What it is not

- **Not a library manager.** It writes tagged files into a directory and stops.
  Whatever serves or syncs your music watches that directory; RipDoctor never
  calls it.
- **Not a restoration tool.** No click removal, no declicking, no noise
  reduction.
- **Not fully automatic.** The last judgement about where a cut goes is yours.

## Development

```
make check     # lint, types, scripts, tests, budgets
make test
```

The page has no build step, so a syntax error in a script blanks it and Python cannot see
that. `make check` parses every script the page loads, as a module, with `scripts/check_js.py`
(`test_the_real_scripts_parse`). With no node on the machine that step says so and steps aside;
CI's `with-tools` job runs it with `--require`, so it always runs somewhere
(`test_ci_has_to_parse_the_pages_scripts_and_make_check_tries_to`).

The core layer computes over decibel envelopes and touches nothing - no
subprocess, no filesystem, no clock - so the whole algorithm is testable without
ffmpeg, a sound card or any audio files. `tests/test_architecture.py` enforces
that boundary.

[docs/decisions.md](docs/decisions.md) records what was decided and why. The
[wiki](https://github.com/bspeelm/RipDoctor/wiki) covers using it rather than
building it.

On Python 3.14 the suite is occasionally unreliable through no fault of its own
(see ADR-019). A failure whose error is impossible - an unknown opcode, a
NameError for something imported at the top of the file - is the interpreter.
Run it again. CI uses 3.11 to 3.13.

## Alternatives

[VinylFlow](https://github.com/olimic1000/vinylflow) (MIT) covers adjacent
ground and is worth a look if RipDoctor is not what you want. It takes a
recording you already made and splits, tags and exports it, with an interactive
waveform editor.

The differences are scope, not quality. VinylFlow does not record, so capture
happens elsewhere and the file is uploaded to it. It previews the resulting
track; RipDoctor is built to judge the boundary itself. It splits on full-band
silence detection with a tunable threshold, which is the approach this project
exists because of.

## Licence

MIT. See [LICENSE](LICENSE).
