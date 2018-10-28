"""Implements "unified" FGD files.

This allows sharing definitions among different engine versions.
"""
import sys
import argparse
from pathlib import Path
from lzma import LZMAFile
from typing import List, Tuple, Set, FrozenSet, Union, Dict

from srctools.fgd import (
    FGD, validate_tags, match_tags,
    EntityDef, EntityTypes,
    HelperTypes, IODef,
    KeyValues, ValueTypes,
)
from srctools.filesys import RawFileSystem


# Chronological order of games.
# If 'since_hl2' etc is used in FGD, all future games also include it.
# If 'until_l4d' etc is used in FGD, only games before include it.

GAMES = [
    ('HLS',  'Half-Life: Source'),
    ('DODS', 'Day of Defeat: Source'),
    ('CSS',  'Counter-Strike: Source'),
    ('HL2',  'Half-Life 2'),
    ('EP1',  'Half-Life 2 Episode 1'),
    ('EP2',  'Half-Life 2 Episode 2'),
    ('TF2',  'Team Fortress 2'),
    ('P1', 'Portal'),
    ('L4D', 'Left 4 Dead'),
    ('L4D2', 'Left 4 Dead 2'),
    ('ASW', 'Alien Swam'),
    ('P2', 'Portal 2'),
    ('CSGO', 'Counter-Strike Global Offensive'),
    ('SFM', 'Source Filmmaker'),
    ('DOTA2', 'Dota 2'),
    ('PUNT', 'PUNT'),
    ('P2DES', 'Portal 2: Desolation'),
]  # type: List[Tuple[str, str]]

GAME_ORDER = [game for game, desc in GAMES]
GAME_NAME = dict(GAMES)

# Specific features that are backported to various games.

FEATURES = {
    'L4D': {'INSTANCING'},
    'TF2': {'INSTANCING', 'PROP_SCALING'},
    'ASW': {'INSTANCING', 'VSCRIPT'},
    'P2': {'INSTANCING', 'VSCRIPT'},
    'CSGO': {'INSTANCING', 'PROP_SCALING', 'VSCRIPT'},
    'P2DES': {'INSTANCING', 'PROP_SCALING', 'VSCRIPT'},
}

ALL_FEATURES = {
    tag.upper() 
    for t in FEATURES.values() 
    for tag in t
}

# Specially handled tags.
TAGS_SPECIAL = {
  'ENGINE',  # Tagged on entries that specify machine-oriented types and defaults.
  'SRCTOOLS', # Implemented by the srctools post-compiler.
}

ALL_TAGS = set()  # type: Set[str]
ALL_TAGS.update(GAME_ORDER)
ALL_TAGS.update(ALL_FEATURES)
ALL_TAGS.update(TAGS_SPECIAL)
ALL_TAGS.update('SINCE_' + t.upper() for t in GAME_ORDER)
ALL_TAGS.update('UNTIL_' + t.upper() for t in GAME_ORDER)


def format_all_tags() -> str:
    """Append a formatted description of all allowed tags to a message."""
    
    return (
        '- Games: {}\n'
        '- SINCE_<game>\n'
        '- UNTIL_<game>\n'
        '- Features: {}\n'
        '- Special: {}\n'
     ).format(
         ', '.join(GAME_ORDER),
         ', '.join(ALL_FEATURES),
        ', '.join(TAGS_SPECIAL),
     )


def expand_tags(tags: FrozenSet[str]) -> FrozenSet[str]:
    """Expand the given tags, producing the full list of tags these will search.

    This adds since_/until_ tags, and values in FEATURES.
    """
    exp_tags = set(tags)
    for tag in tags:
        try:
            exp_tags.update(FEATURES[tag.upper()])
        except KeyError: 
            pass
        try:
            pos = GAME_ORDER.index(tag.upper())
        except ValueError:
            pass
        else:
            exp_tags.update(
                'SINCE_' + tag 
                for tag in GAME_ORDER[:pos+1]
            )
            exp_tags.update(
                'UNTIL_' + tag 
                for tag in GAME_ORDER[pos+1:]
            )
    return frozenset(exp_tags)


def ent_path(ent: EntityDef) -> str:
    """Return the path in the database this entity should be found at."""
    # Very special entity, put in root.
    if ent.classname == 'worldspawn':
        return 'worldspawn.fgd'

    if ent.type is EntityTypes.BASE:
        folder = 'bases'
    elif ent.type is EntityTypes.BRUSH:
        folder = 'brush'
    else:
        folder = 'point/'

    # if '_' in ent.classname:
    #     folder += '/' + ent.classname.split('_', 1)[0]

    return '{}/{}.fgd'.format(folder, ent.classname)


def load_database(dbase: Path) -> FGD:
    """Load the entire database from disk."""
    print('Loading database...')
    fgd = FGD()

    fgd.map_size_min = -16384
    fgd.map_size_max = 16384

    with RawFileSystem(str(dbase)) as fsys:
        for file in dbase.rglob("*.fgd"):
            fgd.parse_file(
                fsys,
                fsys[str(file.relative_to(dbase))],
                eval_bases=False,
            )
            print('.', end='')
    fgd.apply_bases()
    print('\nDone!')
    return fgd


def get_appliesto(ent: EntityDef) -> List[str]:
    """Ensure exactly one AppliesTo() helper is present, and return the args.

    If no helper exists, one will be prepended. Otherwise only the first
    will remain, with the arguments merged together. The same list is
    returned, so it can be viewed or edited.
    """
    pos = None
    applies_to = set()
    for i, (helper_type, args) in enumerate(ent.helpers):
        if helper_type is HelperTypes.EXT_APPLIES_TO:
            if pos is None:
                pos = i
            applies_to.update(args)

    if pos is None:
        pos = 0
    arg_list = sorted(applies_to)
    ent.helpers[:] = [
        tup for tup in
        ent.helpers
        if tup[0] is not HelperTypes.EXT_APPLIES_TO
    ]
    ent.helpers.insert(pos, (HelperTypes.EXT_APPLIES_TO, arg_list))
    return arg_list


def add_tag(tags: FrozenSet[str], new_tag: str) -> FrozenSet[str]:
    """Modify these tags such that they allow the new tag."""
    tag_set = set(tags)
    if new_tag.startswith(('!', '-')):
        tag_set.discard(new_tag[1:])
        tag_set.add(new_tag)
    else:
        tag_set.discard('!' + new_tag.upper())
        tag_set.discard('-' + new_tag.upper())
        if ('+' + new_tag.upper()) not in tag_set:
            tag_set.add(new_tag.upper())

    return frozenset(tag_set)


def action_import(
    dbase: Path,
    engine_tag: str,
    fgd_paths: List[Path],
) -> None:
    """Import an FGD file, adding differences to the unified files."""
    new_fgd = FGD()
    print('Using tag "{}"'.format(engine_tag))

    print('Reading FGDs:'.format(len(fgd_paths)))
    for path in fgd_paths:
        print(path)
        with RawFileSystem(str(path.parent)) as fsys:
            new_fgd.parse_file(fsys, fsys[path.name], eval_bases=False)

    print('\nImporting {} entiti{}...'.format(
        len(new_fgd),
        "y" if len(new_fgd) == 1 else "ies",
    ))
    for new_ent in new_fgd:
        path = dbase / ent_path(new_ent)
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():
            old_fgd = FGD()
            with RawFileSystem(str(path.parent)) as fsys:
                old_fgd.parse_file(fsys, fsys[path.name], eval_bases=False)
            try:
                ent = old_fgd[new_ent.classname]
            except KeyError:
                raise ValueError("Classname not present in FGD!")
            to_export = old_fgd
            # Now merge the two.

            if new_ent.desc not in ent.desc:
                # Temporary, append it.
                ent.desc += '|||' + new_ent.desc

            # Merge helpers. We just combine overall...
            for new_base in new_ent.bases:
                if new_base not in ent.bases:
                    ent.bases.append(new_base)

            for helper in new_ent.helpers:
                # Sorta ew, quadratic search. But helper sizes shouldn't
                # get too big.
                if helper not in ent.helpers:
                    ent.helpers.append(helper)

            for cat in ('keyvalues', 'inputs', 'outputs'):
                cur_map = getattr(ent, cat)  # type: Dict[str, Dict[FrozenSet[str], Union[KeyValues, IODef]]]
                new_map = getattr(new_ent, cat)
                new_names = set()
                for name, tag_map in new_map.items():
                    new_names.add(name)
                    try:
                        orig_tag_map = cur_map[name]
                    except KeyError:
                        # Not present in the old file.
                        cur_map[name] = {
                            add_tag(tag, engine_tag): value
                            for tag, value in tag_map.items()
                        }
                        continue
                    # Otherwise merge, if unequal add the new ones.
                    # TODO: Handle tags in "new" files.
                    for tag, new_value in tag_map.items():
                        for old_tag, old_value in orig_tag_map.items():
                            if old_value == new_value:
                                if tag:
                                    # Already present, modify this tag.
                                    del orig_tag_map[old_tag]
                                    orig_tag_map[add_tag(old_tag, engine_tag)] = new_value
                                # else: Blank tag, keep blank.
                                break
                        else:
                            # Otherwise, we need to add this.
                            orig_tag_map[add_tag(tag, engine_tag)] = new_value

                # Make sure removed items don't apply to the new tag.
                for name, tag_map in cur_map.items():
                    if name not in new_names:
                        cur_map[name] = {
                            add_tag(tag, '!' + engine_tag): value
                            for tag, value in tag_map.items()
                        }

        else:
            # No existing one, just set appliesto.
            ent = new_ent
            # We just write this entity in.
            to_export = new_ent

        applies_to = get_appliesto(ent)
        if engine_tag not in applies_to:
            applies_to.append(engine_tag)

        with open(path, 'w') as f:
            to_export.export(f)

        print('.', end='', flush=True)
    print()


def action_export(
    dbase: Path,
    tags: FrozenSet[str],
    output_path: Path,
    as_binary: bool,
    engine_mode: bool,
) -> None:
    """Create an FGD file using the given tags."""
    
    if engine_mode:
        tags = frozenset({'ENGINE'})
    else:
        tags = expand_tags(tags)

    print('Tags expanded to: {}'.format(', '.join(tags)))

    fgd = load_database(dbase)

    if engine_mode:
        # In engine mode, we don't care about specific games.
        print('Collapsing bases...')
        fgd.collapse_bases()

        tags_empty = frozenset()
        tags_engine = frozenset({'ENGINE'})

        print('Merging tags...')
        for ent in fgd:
            # Strip applies-to helper and ordering helper.
            ent.helpers[:] = [
                helper for helper in ent.helpers
                if helper[0] is not HelperTypes.EXT_APPLIES_TO and
                   helper[0] is not HelperTypes.EXT_ORDERBY
            ]
            for category in [ent.inputs, ent.outputs, ent.keyvalues]:
                # For each category, check for what value we want to keep.
                # If only one, we keep that.
                # If there's an "ENGINE" tag, that's specifically for us.
                # Otherwise, warn if there's a type conflict.
                # If the final value is choices, warn too (not really a type).
                for key, tag_map in category.items():
                    if len(tag_map) == 1:
                        [value] = tag_map.values()
                    elif tags_engine in tag_map:
                        value = tag_map[tags_engine]
                        if value.type is ValueTypes.CHOICES:
                            raise ValueError(
                                '{}.{}: Engine tags cannot be '
                                'CHOICES!'.format(ent.classname, key)
                            )
                    else:
                        # More than one tag.
                        # IODef and KeyValues have a type attr.
                        types = {val.type for val in tag_map.values()}
                        if len(types) > 2:
                            print('{}.{} has multiple types! ({})'.format(
                                ent.classname,
                                key,
                                ', '.join([typ.value for typ in types])
                            ))
                        # Pick the one with shortest tags arbitrarily.
                        value = sorted(
                            tag_map.items(),
                            key=lambda t: len(t[0]),
                        )[0][1]  # type: Union[IODef, KeyValues]

                    if value.type is ValueTypes.CHOICES:
                        print(
                            '{}.{} uses CHOICES type, '
                            'provide ENGINE '
                            'tag!'.format(ent.classname, key)
                        )
                        if isinstance(value, KeyValues):
                            try:
                                for choice_val, name, tag in value.val_list:
                                    int(choice_val)
                            except ValueError:
                                # Not all are ints, it's a string.
                                value.type = ValueTypes.STRING
                            else:
                                value.type = ValueTypes.INT
                            value.val_list = None

                    # Blank this, it's not that useful.
                    value.desc = ''

                    category[key] = {tags_empty: value}

    else:
        print('Culling incompatible entities...')

        ents = list(fgd.entities.values())
        fgd.entities.clear()

        for ent in ents:
            applies_to = get_appliesto(ent)
            if match_tags(tags, applies_to):
                fgd.entities[ent.classname] = ent
    
                # Strip applies-to helper.
                ent.helpers[:] = [
                    helper for helper in ent.helpers
                    if helper[0] is not HelperTypes.EXT_APPLIES_TO
                ]
                ent.strip_tags(tags)

        print('Culled entities, merging bases...')

        fgd.collapse_bases()

    print('Exporting...')

    # Remove all base entities.
    fgd.entities = {
        clsname: ent
        for clsname, ent in fgd.entities.items()
        if ent.type is not EntityTypes.BASE
    }

    if as_binary:
        with open(output_path, 'wb') as f, LZMAFile(f, 'w') as comp:
            fgd.serialise(comp)
    else:
        with open(output_path, 'w') as f:
            fgd.export(f)

def main(args: List[str]=None):
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Manage a set of unified FGDs, sharing configs "
                    "between engine versions.",

    )
    parser.add_argument(
        "-d", "--database",
        default="fgd/",
        help="The folder to write the FGD files to or from."
    )
    subparsers = parser.add_subparsers(dest="mode")

    parser_exp = subparsers.add_parser(
        "export",
        help=action_export.__doc__,
        aliases=["exp", "i"],
    )

    parser_exp.add_argument(
        "-o", "--output",
        default="output.fgd",
        help="Destination FGD filename."
    )
    parser_exp.add_argument(
        "-e", "--engine",
        action="store_true",
        help="If set, produce FGD for parsing by script. "
             "This includes all keyvalues regardless of tags, "
             "to allow parsing VMF/BSP files. Overrides tags if "
             " provided.",
    )
    parser_exp.add_argument(
        "-b", "--binary",
        action="store_true",
        help="If set, produce a binary format used by Srctools.",
    )
    parser_exp.add_argument(
        "tags",
        nargs="*",
        help="Tags to include in the output.",
        default=None,
    )

    parser_imp = subparsers.add_parser(
        "import",
        help=action_import.__doc__,
        aliases=["imp", "i"],
    )
    parser_imp.add_argument(
        "engine",
        type=str.upper,
        choices=GAME_ORDER,
        help="Engine to mark this FGD set as supported by.",
    )
    parser_imp.add_argument(
        "fgd",
        nargs="+",
        type=Path,
        help="The FGD files to import. "
    )

    result = parser.parse_args(args)

    if result.mode is None:
        parser.print_help()
        return

    dbase = Path(result.database).resolve()
    dbase.mkdir(parents=True, exist_ok=True)

    if result.mode in ("import", "imp", "i"):
        action_import(
            dbase,
            result.engine,
            result.fgd,
        )
    elif result.mode in ("export", "exp", "e"):
        # Engine means tags are ignored.
        # Non-engine means tags must be specified!
        if result.engine:
            if result.tags:
                print("Tags ignored in --engine mode...", file=sys.stderr)
            result.tags = ['ENGINE']
        elif not result.tags:
            parser.error("At least one tag must be specified!")
            
        tags = validate_tags(result.tags)
        
        for tag in tags:
            if tag not in ALL_TAGS:
                parser.error(
                    'Invalid tag "{}"! Allowed tags: \n'.format(tag) +
                    format_all_tags()
                )
        action_export(
            dbase,
            tags,
            result.output,
            result.binary,
            result.engine,
        )
    else:
        raise AssertionError("Unknown mode! (" + result.mode + ")")


if __name__ == '__main__':
    main(sys.argv[1:])

    #for game in GAME_ORDER:
    #    print('\n'+ game + ':')
    #    main(['export', '-o', 'fgd_out/' + game + '.fgd', game])
