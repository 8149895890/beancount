"""Format converter reports.

This module contains reports that can convert an input file into other formats,
such as Ledger.
"""
__author__ = "Martin Blais <blais@furius.ca>"

import re
import io

from beancount.core import amount
from beancount.core import data
from beancount.core import position
from beancount.core import interpolate
from beancount.reports import report


ROUNDING_ACCOUNT = 'Equity:Rounding'


def quote(match):
    """Add quotes around a re.MatchObject.

    Args:
      match: A MatchObject from the re module.
    Returns:
      A quoted string of the match contents.
    """
    currency = match.group(1)
    return '"{}"'.format(currency) if re.search(r'[0-9\.]', currency) else currency


def quote_currency(string):
    """Quote all the currencies with numbers from the given string.

    Args:
      string: A string of text.
    Returns:
      A string of text, with the commodity expressions surrounded with quotes.
    """
    return re.sub(r'\b([A-Z][A-Z0-9\'\.\_\-]{0,10}[A-Z0-9])\b', quote, string)


def postings_by_type(entry):
    """Split up the postings by simple, at-cost, at-price.

    Args:
      entry: An instance of Transaction.
    Returns:
      A tuple of simple postings, postings with price conversions, postings held at cost.
    """
    postings_at_cost = []
    postings_at_price = []
    postings_simple = []
    for posting in entry.postings:
        if posting.position.lot.cost:
            accumlator = postings_at_cost
        elif posting.price:
            accumlator = postings_at_price
        else:
            accumlator = postings_simple
        accumlator.append(posting)

    return (postings_simple, postings_at_price, postings_at_cost)


def split_currency_conversions(entry):
    """If the transcation has a mix of conversion at cost and a
    currency conversion, split the transction into two transactions: one
    that applies the currency conversion in the same account, and one
    that uses the other currency without conversion.

    This is required because Ledger does not appear to be able to grok a
    transaction like this one:

      2014-11-02 * "Buy some stock with foreign currency funds"
        Assets:CA:Investment:GOOG          5 GOOG {520.0 USD}
        Expenses:Commissions            9.95 USD
        Assets:CA:Investment:Cash   -2939.46 CAD @ 0.8879 USD

    HISTORICAL NOTE: Adding a price directive on the first posting above makes
    Ledger accept the transaction. So we will not split the transaction here
    now. However, since Ledger's treatment of this type of conflict is subject
    to revision (See http://bugs.ledger-cli.org/show_bug.cgi?id=630), we will
    keep this code around, it might become useful eventually. See
    https://groups.google.com/d/msg/ledger-cli/35hA0Dvhom0/WX8gY_5kHy0J for
    details of the discussion.

    Args:
      entry: An instance of Transaction.
    Returns:
      A pair of
        converted: boolean, true if a conversion was made.
        entries: A list of the original entry if converted was False,
          or a list of the split converted entries if True.
    """
    assert isinstance(entry, data.Transaction)

    (postings_simple, postings_at_price, postings_at_cost) = postings_by_type(entry)

    converted = postings_at_cost and postings_at_price
    if converted:
        # Generate a new entry for each currency conversion.
        new_entries = []
        replacement_postings = []
        for posting_orig in postings_at_price:
            weight = interpolate.get_posting_weight(posting_orig)
            simple_position = position.Position(position.Lot(weight.currency, None, None),
                                                weight.number)
            posting_pos = data.Posting(None, posting_orig.account, simple_position,
                                       None, None)
            posting_neg = data.Posting(None, posting_orig.account, -simple_position,
                                       None, None)

            currency_entry = data.entry_replace(
                entry,
                postings=[posting_orig, posting_neg],
                narration=entry.narration + ' (Currency conversion)')
            new_entries.append(currency_entry)
            replacement_postings.append(posting_pos)

        converted_entry = data.entry_replace(entry, postings=(
            postings_at_cost + postings_simple + replacement_postings))
        new_entries.append(converted_entry)
    else:
        new_entries = [entry]

    return converted, new_entries


class LedgerReport(report.Report):
    """Print out the entries in a format that can be parsed by Ledger."""

    names = ['ledger']
    default_format = 'ledger'

    def render_ledger(self, entries, errors, options_map, file):
        ledger_printer = LedgerPrinter()
        for entry in entries:
            file.write(ledger_printer(entry))
            file.write('\n')


class LedgerPrinter:
    "Multi-method for printing directives in Ledger format."

    # pylint: disable=invalid-name

    @classmethod
    def __call__(cls, obj):
        oss = io.StringIO()
        getattr(cls, obj.__class__.__name__)(cls, obj, oss)
        return oss.getvalue()

    def Transaction(cls, entry, oss):
        strings = []

        # Insert a posting to absorb the residual if necessary. This is
        # sometimes needed because Ledger bases its balancing precision on the
        # *last* number of digits used on that currency. This is believed to be
        # a bug, so instead, we simply insert a rounding account to absorb the
        # residual and precisely balance the transaction.
        entry = interpolate.fill_residual_posting(entry, ROUNDING_ACCOUNT)

        if entry.tags:
            for tag in sorted(entry.tags):
                strings.append(';; Tag: #{}'.format(tag))
        if entry.links:
            for link in sorted(entry.links):
                strings.append(';; Link: ^{}'.format(link))

        # Compute the string for the payee and narration line.
        if entry.payee:
            strings.append('{} |'.format(entry.payee))
        if entry.narration:
            strings.append(entry.narration)

        oss.write('{e.date:%Y/%m/%d} {flag} {}\n'.format(' '.join(strings),
                                                         flag=entry.flag or '',
                                                         e=entry))

        for posting in entry.postings:
            cls.Posting(cls, posting, oss)

    def Posting(_, posting, oss):
        flag = '{} '.format(posting.flag) if posting.flag else ''
        assert posting.account is not None

        flag_posting = '{:}{:62}'.format(flag, posting.account)

        if posting.position:
            amount_str, cost_str = posting.position.strs()
        else:
            amount_str, cost_str = '', ''

        if posting.price is not None:
            price_str = '@ {}'.format(posting.price.str(amount.MAXDIGITS_PRINTER))
        else:
            # Figure out if we need to insert a price on a posting held at cost.
            # See https://groups.google.com/d/msg/ledger-cli/35hA0Dvhom0/WX8gY_5kHy0J
            (postings_simple,
             postings_at_price,
             postings_at_cost) = postings_by_type(posting.entry)

            if postings_at_price and postings_at_cost and posting.position.lot.cost:
                price_str = '@ {}'.format(
                    posting.position.lot.cost.str(amount.MAXDIGITS_PRINTER))
            else:
                price_str = ''

        posting_str = '  {:64} {:>16} {:>16} {:>16}'.format(flag_posting,
                                                            quote_currency(amount_str),
                                                            quote_currency(cost_str),
                                                            quote_currency(price_str))
        oss.write(posting_str.rstrip())

        oss.write('\n')

    def Balance(_, entry, oss):
        # We cannot output balance directive equivalents because Ledger only
        # supports file assertions and not dated assertions. See "Balance
        # Assertions for Beancount" for details:
        # https://docs.google.com/document/d/1vyemZFox47IZjuBrT2RjhSHZyTgloYOUeJb73RxMRD0/
        pass

    def Note(_, entry, oss):
        oss.write(';; Note: {e.date:%Y/%m/%d} {e.account} {e.comment}\n'.format(e=entry))

    def Document(_, entry, oss):
        oss.write(';; Document: {e.date:%Y/%m/%d} {e.account} {e.filename}\n'.format(
            e=entry))

    def Pad(_, entry, oss):
        # Note: We don't need to output these because when we're loading the
        # Beancount file explicit padding entries will be generated
        # automatically, thus balancing the accounts. Ledger does not support
        # automatically padding, so we can just output this as a comment.
        oss.write(';; Pad: {e.date:%Y/%m/%d} {e.account} {e.source_account}\n'.format(
            e=entry))

    def Open(_, entry, oss):
        oss.write('account {e.account:47}\n'.format(e=entry))
        if entry.currencies:
            oss.write('  assert {}\n'.format(' | '.join('commodity == "{}"'.format(currency)
                                                        for currency in entry.currencies)))

    def Close(_, entry, oss):
        oss.write(';; Close: {e.date:%Y/%m/%d} close {e.account}\n'.format(e=entry))

    def Price(_, entry, oss):
        price_directive = (
            'P {e.date:%Y/%m/%d} 00:00:00 {e.currency:<16} {amount:>16}\n'.format(
            e=entry, amount=str(entry.amount)))
        oss.write(quote_currency(price_directive))

    def Event(_, entry, oss):
        oss.write(
            ';; Event: {e.date:%Y/%m/%d} "{e.type}" "{e.description}"\n'.format(e=entry))


class HLedgerReport(report.Report):
    """Print out the entries in a format that can be parsed by HLedger."""

    names = ['hledger']
    default_format = 'hledger'

    def render_hledger(self, entries, errors, options_map, file):
        hledger_printer = HLedgerPrinter()
        for entry in entries:
            file.write(hledger_printer(entry))
            file.write('\n')


class HLedgerPrinter(LedgerPrinter):
    "Multi-method for printing directives in HLedger format."

    # pylint: disable=invalid-name

    @classmethod
    def __call__(cls, obj):
        oss = io.StringIO()
        getattr(cls, obj.__class__.__name__)(cls, obj, oss)
        return oss.getvalue()

    def Posting(_, posting, oss):
        flag = '{} '.format(posting.flag) if posting.flag else ''
        assert posting.account is not None

        flag_posting = '{:}{:62}'.format(flag, posting.account)

        if posting.position:
            amount_str, cost_str = posting.position.strs()
            if cost_str:
                # Convert the cost as a price entry, that's what HLedger appears to want.
                cost_str = '@ {}'.format(cost_str.lstrip('{').rstrip('}'))
        else:
            amount_str, cost_str = '', ''

        price_str = ('@ {}'.format(posting.price.str(amount.MAXDIGITS_PRINTER))
                     if posting.price is not None
                     else '')

        posting_str = '  {:64} {:>16} {:>16}'.format(flag_posting,
                                                     quote_currency(amount_str),
                                                     quote_currency(cost_str))
        oss.write(posting_str.rstrip())

        oss.write('\n')

    def Open(_, entry, oss):
        # Not supported by HLedger AFAIK.
        oss.write(';; Open: {e.date:%Y/%m/%d} close {e.account}\n'.format(e=entry))


__reports__ = [
    LedgerReport,
    HLedgerReport,
    ]