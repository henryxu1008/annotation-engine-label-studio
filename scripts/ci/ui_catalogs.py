"""Compile the actual two root catalogs into job-private storage, leaving tracked .mo untouched."""
import argparse
import gettext
import os
import shutil
from pathlib import Path

from ui_common import ROOT, file_hash, read_json, require, write_json

LOCALES = ('en_US', 'zh_Hans')


def validate_catalogs(private):
    private = Path(private)
    base = private / 'catalogs' / 'locale'
    proof = read_json(private / 'catalogs.json')
    require(
        proof.get('schema') == 'ui-catalogs/v1' and set(proof.get('locales', {})) == set(LOCALES),
        'Catalog proof incomplete',
    )
    for locale in LOCALES:
        source = ROOT / 'label_studio/locale' / locale / 'LC_MESSAGES/django.po'
        target = base / locale / 'LC_MESSAGES/django.mo'
        record = proof['locales'][locale]
        require(
            source.is_file()
            and target.is_file()
            and record['source_sha256'] == file_hash(source)
            and record['mo_sha256'] == file_hash(target),
            'Catalog source/output mismatch',
        )
        with target.open('rb') as f:
            translations = gettext.GNUTranslations(f)
        require(len(translations._catalog) > 1, 'Empty compiled catalog')
    return base


def compile_gnu(base):
    """Use Django discovery from an isolated conventional locale tree."""
    os.environ.pop('DJANGO_SETTINGS_MODULE', None)
    from django.conf import settings

    settings.configure(USE_I18N=True, LOCALE_PATHS=[str(base)], INSTALLED_APPS=[], SECRET_KEY='ci-catalogs-only')
    import django

    django.setup()
    from django.core.management import call_command

    # compilemessages ignores LOCALE_PATHS without DJANGO_SETTINGS_MODULE.
    # Its conventional "locale" directory discovery works without product settings.
    os.chdir(base.parent)
    call_command('compilemessages', locale=list(LOCALES), verbosity=1)


def compile_catalogs(private):
    private = Path(private)
    base = private / 'catalogs' / 'locale'
    base.mkdir(parents=True, exist_ok=False)
    for locale in LOCALES:
        source = ROOT / 'label_studio/locale' / locale / 'LC_MESSAGES/django.po'
        require(source.is_file(), 'Required root locale source missing')
        target = base / locale / 'LC_MESSAGES/django.po'
        target.parent.mkdir(parents=True)
        shutil.copyfile(source, target)
    if os.name == 'nt':
        from babel.messages.mofile import write_mo
        from babel.messages.pofile import read_po

        for locale in LOCALES:
            source = base / locale / 'LC_MESSAGES/django.po'
            with source.open('rb') as f:
                catalog = read_po(f, locale=locale)
            with source.with_suffix('.mo').open('xb') as f:
                write_mo(f, catalog)
        method = 'Babel native Windows acceptance; GNU/Linux execution separate'
    else:
        # Django's existing GNU gettext compiler, isolated from repository/app locale outputs.
        compile_gnu(base)
        method = 'Django compilemessages / GNU gettext'
    proof = {'schema': 'ui-catalogs/v1', 'method': method, 'locales': {}}
    for locale in LOCALES:
        source = ROOT / 'label_studio/locale' / locale / 'LC_MESSAGES/django.po'
        output = base / locale / 'LC_MESSAGES/django.mo'
        require(output.is_file(), 'Compiler did not produce required language')
        proof['locales'][locale] = {'source_sha256': file_hash(source), 'mo_sha256': file_hash(output)}
    write_json(private / 'catalogs.json', proof)
    validate_catalogs(private)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--private', type=Path, required=True)
    compile_catalogs(parser.parse_args().private)
