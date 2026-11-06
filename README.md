# RipDoctor

Record a vinyl side, split it into tracks, tag them and file them in your music
library.

![The cut panel. A side's waveform in blue, the same side measured in 1-3 kHz in
orange below it, the gap threshold as a dashed line, and the track boundaries as
vertical markers.](docs/images/cut_panel.png)

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

![The rip panel while a side is recording. Elapsed time, the music level, and a
bar showing which zone the 1-3 kHz level is in.](docs/images/rip_panel.png)

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

## Tapes

A tape is one uncut recording of songs that belong to different albums, such as a
mixtape in a single file. Its steps are the same as a record's, and only tagging
and filing work per song. The [Tapes wiki page](https://github.com/bspeelm/RipDoctor/wiki/Tapes)
walks through it. The name in brackets after each claim is the test that proves it.

Its boundaries are checked on the same waveform editor a record's are
(`test_a_tape_s_waveform_is_the_rip_s_editor_and_player_and_not_a_copy_of_them`).
A boundary you drag is yours and nothing moves it afterwards
(`test_a_move_by_hand_is_the_person_s_and_nothing_afterwards_moves_it`), and one that
would pass the song beside it is refused and goes back
(`test_a_move_by_hand_past_a_neighbour_or_the_ends_is_refused`). The cut waits until
every doubt is moved, accepted or skipped
(`test_accepting_settles_a_doubt_and_the_cut_waits_until_it_is_settled`).

The page is one pipeline rail, one waveform and one table of songs, with a row that
opens (`test_a_tape_s_songs_are_in_one_table`,
`test_the_pipeline_rail_runs_in_order_with_archive_last`). Once the audio and the list are
both in, boundaries and albums follow by themselves
(`test_a_tape_goes_on_by_itself_once_its_audio_and_list_are_both_in`). A cut needs settled
boundaries, and filing also needs every song's album
(`test_a_cut_is_not_said_to_wait_on_an_album_and_a_file_is`). A row chooses its album from
one line per option, and a choice more than 3 s from the list asks first, in that line
(`test_an_option_is_one_line_of_columns_with_no_edition_jargon`,
`test_a_release_far_from_the_list_is_asked_about_and_sent_again`).

A filed song with no cover says so, and one uploaded image is embedded and set as its
album's art (`test_a_filed_song_with_no_cover_says_so_and_one_with_a_cover_does_not`,
`test_one_upload_embeds_and_sets_the_album_s_art_for_all_its_songs`). The Playlist step says
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
  (`test_a_row_with_no_recording_id_is_for_a_person_to_confirm_on_a_pinned_song`).
- **A filed song keeps its release**
  (`test_a_song_in_the_library_keeps_the_release_it_was_filed_as`), and a song whose
  filing failed can still be changed
  (`test_a_song_whose_filing_failed_can_still_be_changed`).
- **The playlist is an `.m3u8` file** in the library's `Playlists` folder
  (`test_the_file_lands_in_the_library_playlists_folder_as_utf8`), with paths
  relative to it (`test_the_text_is_the_header_then_relative_lines_in_list_order_with_lf`).
- **The uncut file is archived only once every song resolves**
  (`test_archive_is_refused_with_every_reason_while_a_song_is_unresolved`).

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
make check     # lint, types, tests, budgets
make test
```

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
