import functools
import os
import threading
from time import sleep
from Constants import MUTEZ, VERSION, EXIT_PAYMENT_TYPE, PaymentStatus
from calc.calculate_phaseMapping import CalculatePhaseMapping
from calc.calculate_phaseMerge import CalculatePhaseMerge
from calc.calculate_phaseZeroBalance import CalculatePhaseZeroBalance
from log_config import main_logger
from model.reward_log import cmp_by_type_balance, TYPE_MERGED, TYPE_FOUNDER, TYPE_OWNER, TYPE_DELEGATOR
from pay.batch_payer import BatchPayer
from stats.stats_publisher import stats_publisher
from util.csv_payment_file_parser import CsvPaymentFileParser
from util.dir_utils import payment_report_file_path, get_busy_file

logger = main_logger.getChild("payment_consumer")


def count_and_log_failed(payment_logs):

    nb_paid = nb_failed = nb_injected = 0

    for pymnt_itm in payment_logs:
        if pymnt_itm.paid == PaymentStatus.PAID:
            nb_paid += 1
        elif pymnt_itm.paid == PaymentStatus.FAIL:
            nb_failed += 1
        elif pymnt_itm.paid == PaymentStatus.INJECTED:
            nb_injected += 1

    return nb_paid, nb_failed, nb_injected


class PaymentConsumer(threading.Thread):
    def __init__(self, name, payments_dir, key_name, payments_queue, node_addr, client_manager,
                 network_config, plugins_manager, rewards_type, args=None, dry_run=None, reactivate_zeroed=True,
                 delegator_pays_ra_fee=True, delegator_pays_xfer_fee=True, dest_map=None, publish_stats=True):
        super(PaymentConsumer, self).__init__()

        self.dest_map = dest_map if dest_map else {}
        self.name = name
        self.payments_dir = payments_dir
        self.key_name = key_name
        self.payments_queue = payments_queue
        self.node_addr = node_addr
        self.dry_run = dry_run
        self.client_manager = client_manager
        self.reactivate_zeroed = reactivate_zeroed
        self.delegator_pays_xfer_fee = delegator_pays_xfer_fee
        self.delegator_pays_ra_fee = delegator_pays_ra_fee
        self.publish_stats = publish_stats
        self.args = args
        self.network_config = network_config
        self.plugins_manager = plugins_manager
        self.rewards_type = rewards_type

        logger.debug('Consumer "%s" created', self.name)

        return

    def run(self):
        running = True
        while running:
            # Wait until a reward is present
            payment_batch = self.payments_queue.get(True)

            running = self._consume_batch(payment_batch)

        logger.debug("Consumer returning...")

        return

    def _consume_batch(self, payment_batch):
        try:
            payment_items = payment_batch.batch

            if len(payment_items) == 0:
                logger.debug("Batch is empty, ignoring...")
                return True

            if payment_items[0].type == EXIT_PAYMENT_TYPE:
                logger.warn("Exit signal received. Terminating...")
                return False

            sleep(1)

            pymnt_cycle = payment_batch.cycle

            logger.info("Starting payments for cycle {}".format(pymnt_cycle))

            # Filter out non-payable items
            payment_items = [pi for pi in payment_items if pi.payable]
            already_paid_items = [pi for pi in payment_items if pi.paid.is_processed()]
            payment_items = [pi for pi in payment_items if not pi.paid.is_processed()]

            # Handle remapping of payment to alternate address
            phaseMapping = CalculatePhaseMapping()
            payment_items = phaseMapping.calculate(payment_items, self.dest_map)

            # Merge payments to same address
            phaseMerge = CalculatePhaseMerge()
            payment_items = phaseMerge.calculate(payment_items)

            # Filter zero-balance addresses based on config
            phaseZeroBalance = CalculatePhaseZeroBalance()
            payment_items = phaseZeroBalance.calculate(payment_items, self.reactivate_zeroed)

            payment_items.sort(key=functools.cmp_to_key(cmp_by_type_balance))

            batch_payer = BatchPayer(self.node_addr, self.key_name, self.client_manager,
                                     self.delegator_pays_ra_fee, self.delegator_pays_xfer_fee,
                                     self.network_config, self.plugins_manager,
                                     self.dry_run)

            # 3- do the payment
            payment_logs, total_attempts, total_payout_amount, number_future_payable_cycles = \
                batch_payer.pay(payment_items, dry_run=self.dry_run)

            # override batch data
            payment_batch.batch = payment_logs

            # 4- count failed payments
            nb_paid, nb_failed, nb_unknown = count_and_log_failed(payment_logs)

            # 5- create payment report file
            report_file = self.create_payment_report(nb_failed, payment_logs, pymnt_cycle, already_paid_items)

            # 6- Clean failure reports
            self.clean_failed_payment_reports(pymnt_cycle, nb_failed == 0)

            # 7- notify batch producer
            if nb_failed == 0:
                if payment_batch.producer_ref:
                    payment_batch.producer_ref.on_success(payment_batch)
            else:
                if payment_batch.producer_ref:
                    payment_batch.producer_ref.on_fail(payment_batch)

            # 8- send notification via plugins
            if total_attempts > 0:

                subject = "Reward Payouts for Cycle {:d}".format(pymnt_cycle)

                status = ''
                if nb_failed == 0 and nb_unknown == 0:
                    status = status + 'Completed Successfully!'
                else:
                    status = status + 'attempted'
                    if nb_failed > 0:
                        status = status + ", {:d} failed".format(nb_failed)
                    if nb_unknown > 0:
                        status = status + ", {:d} injected but final state not known".format(nb_unknown)
                subject = subject + ' ' + status

                admin_message = "The current payout account balance is expected to last for the next {:d} cycle(s)!".format(number_future_payable_cycles)

                # Payout notification receives cycle, rewards total, number of delegators
                self.plugins_manager.send_payout_notification(pymnt_cycle, total_payout_amount, (nb_paid + nb_failed + nb_unknown))

                # Admin notification receives subject, message, CSV report, raw log objects
                self.plugins_manager.send_admin_notification(subject, admin_message, [report_file], payment_logs)

            # 9- publish anonymous stats
            if self.publish_stats and self.args and not self.dry_run:
                stats_dict = self.create_stats_dict(nb_failed, nb_unknown, pymnt_cycle, payment_logs, total_attempts)
                stats_publisher(stats_dict)

        except Exception:
            logger.error("Error at reward payment", exc_info=True)

        return True

    def clean_failed_payment_reports(self, payment_cycle, success):
        # 1- generate path of a assumed failure report file
        # if it exists and payments were successful, remove it
        failure_report_file = payment_report_file_path(self.payments_dir, payment_cycle, 1)
        if success and os.path.isfile(failure_report_file):
            os.remove(failure_report_file)
        # 2- generate path of a assumed busy failure report file
        # if it exists, remove it
        ###
        # remove file failed/cycle.csv.BUSY file;
        #  - if payment attempt was successful it is not needed anymore,
        #  - if payment attempt was un-successful, new failedY/cycle.csv is already created.
        # Thus  failed/cycle.csv.BUSY file is not needed and removing it is fine.
        failure_report_busy_file = get_busy_file(failure_report_file)
        if os.path.isfile(failure_report_busy_file):
            os.remove(failure_report_busy_file)

    #
    # create report file
    def create_payment_report(self, nb_failed, payment_logs, payment_cycle, already_paid_items):

        logger.info("Processing completed for {} payment items{}.".format(len(payment_logs), ", {} failed".format(nb_failed) if nb_failed > 0 else ""))
        logger.debug("Adding {} already paid items to the report".format(len(already_paid_items)))

        payouts = already_paid_items + payment_logs

        successful_payouts = [x for x in payouts if x.paid != PaymentStatus.FAIL]
        unsuccessful_payouts = [x for x in payouts if x.paid == PaymentStatus.FAIL]

        report_file = payment_report_file_path(self.payments_dir, payment_cycle, 0)
        CsvPaymentFileParser().write(report_file, successful_payouts)
        logger.info("Payment report is created at '{}'".format(report_file))

        if nb_failed > 0:
            report_file = payment_report_file_path(self.payments_dir, payment_cycle, nb_failed)
            CsvPaymentFileParser().write(report_file, unsuccessful_payouts)
            logger.info("Payment report is created at '{}'".format(report_file))

        for pl in payment_logs:
            logger.debug("Payment done for address {:s} type {:s} amount {:>10.6f} paid {:s}".format(pl.address, pl.type, pl.amount / MUTEZ, pl.paid))

        return report_file

    def create_stats_dict(self, nb_failed, nb_unknown, payment_cycle, payment_logs, total_attempts):

        from uuid import uuid1

        n_f_type = len([pl for pl in payment_logs if pl.type == TYPE_FOUNDER])
        n_o_type = len([pl for pl in payment_logs if pl.type == TYPE_OWNER])
        n_d_type = len([pl for pl in payment_logs if pl.type == TYPE_DELEGATOR])
        n_m_type = len([pl for pl in payment_logs if pl.type == TYPE_MERGED])

        stats_dict = {}
        stats_dict['uuid'] = str(uuid1())
        stats_dict['cycle'] = payment_cycle
        stats_dict['network'] = self.args.network
        stats_dict['total_amount'] = int(sum([rl.amount for rl in payment_logs]) / MUTEZ)
        stats_dict['nb_pay'] = int(len(payment_logs))
        stats_dict['nb_failed'] = nb_failed
        stats_dict['nb_unknown'] = nb_unknown
        stats_dict['total_attmpts'] = total_attempts
        stats_dict['nb_founders'] = n_f_type
        stats_dict['nb_owners'] = n_o_type
        stats_dict['nb_merged'] = n_m_type
        stats_dict['nb_delegators'] = n_d_type
        stats_dict['pay_xfer_fee'] = 1 if self.delegator_pays_xfer_fee else 0
        stats_dict['pay_ra_fee'] = 1 if self.delegator_pays_ra_fee else 0
        if self.rewards_type.isIdeal():
            stats_dict['rewards_type'] = "I"
        elif self.rewards_type.isEstimated():
            stats_dict['rewards_type'] = "E"
        elif self.rewards_type.isActual():
            stats_dict['rewards_type'] = "A"
        else:
            stats_dict['rewards_type'] = "A"
            logger.info("Reward type is set to actual by default - please check your configuration")
        stats_dict['trdver'] = str(VERSION)

        if self.args:
            stats_dict['m_run'] = 1 if self.args.background_service else 0
            stats_dict['m_prov'] = self.args.reward_data_provider
            stats_dict['m_relov'] = self.args.release_override if self.args.release_override else 0
            stats_dict['m_offset'] = self.args.payment_offset if self.args.payment_offset else 0
            stats_dict['m_docker'] = 1 if self.args.docker else 0

        return stats_dict
