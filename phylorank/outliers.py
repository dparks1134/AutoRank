###############################################################################
#                                                                             #
#    This program is free software: you can redistribute it and/or modify     #
#    it under the terms of the GNU General Public License as published by     #
#    the Free Software Foundation, either version 3 of the License, or        #
#    (at your option) any later version.                                      #
#                                                                             #
#    This program is distributed in the hope that it will be useful,          #
#    but WITHOUT ANY WARRANTY; without even the implied warranty of           #
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the            #
#    GNU General Public License for more details.                             #
#                                                                             #
#    You should have received a copy of the GNU General Public License        #
#    along with this program. If not, see <http://www.gnu.org/licenses/>.     #
#                                                                             #
###############################################################################

import os
import sys
import logging
import random
from collections import defaultdict, namedtuple

from phylorank.rel_dist import RelativeDistance
from phylorank.common import (read_taxa_file,
                              filter_taxa_for_dist_inference,
                              is_integer,
                              get_phyla_lineages)
from phylorank.newick import parse_label

from biolib.taxonomy import Taxonomy
from biolib.plots.abstract_plot import AbstractPlot
from biolib.external.execute import check_dependencies

from numpy import (mean as np_mean,
                   std as np_std,
                   median as np_median,
                   abs as np_abs,
                   array as np_array,
                   arange as np_arange,
                   linspace as np_linspace,
                   percentile as np_percentile,
                   ones_like as np_ones_like,
                   histogram as np_histogram)

from scipy.stats import norm

import dendropy
import mpld3


class Outliers(AbstractPlot):
    """Identify outliers based on relative distances.

    A named group is considered an outlier if it falls
    far from the median of all other groups at the same
    rank. Since the relative distance of a group from
    the root depending on the rooting, the input tree
    is rooted on all phyla. Results are reported for
    each of these rooting and a consensus file produced
    indicating the median distance over all rootings.
    """

    def __init__(self, dpi=96):
        """Initialize."""
        self.logger = logging.getLogger()

        Options = namedtuple('Options', 'width height tick_font_size label_font_size dpi')
        options = Options(6, 6, 12, 12, 96)

        AbstractPlot.__init__(self, options)
        
        self.dpi = dpi
        
    def root_with_outgroup(self, input_tree, taxonomy, outgroup_taxa):
        """Reroot the tree using the given outgroup.

        Parameters
        ----------
        input_tree : Dendropy Tree
          Tree to rerooted.
        taxonomy : dict
          Taxonomy for taxa.
        outgroup_taxa : str
          Desired outgroup.
          
        Returns
        -------
        Dendropy Tree
            Deep-copy of original tree rerooted on outgroup.
        """
        
        new_tree = input_tree.clone()
        
        outgroup = set()
        for genome_id, taxa in taxonomy.iteritems():
            if outgroup_taxa in taxa:
                outgroup.add(genome_id)
        self.logger.info('Identifying %d genomes in the outgroup.' % len(outgroup))

        outgroup_in_tree = set()
        ingroup_in_tree = set()
        for n in new_tree.leaf_node_iter():
            if n.taxon.label in outgroup:
                outgroup_in_tree.add(n.taxon)
            else:
                ingroup_in_tree.add(n)
        self.logger.info('Identified %d outgroup taxa in the tree.' % len(outgroup_in_tree))

        if len(outgroup_in_tree) == 0:
            self.logger.warning('No outgroup taxa identified in the tree.')
            self.logger.warning('Tree was not rerooted.')
            sys.exit(0)
            
        # There is a complication here. We wish to find the MRCA of the outgroup
        # taxa. Finding the MRCA requires a rooted tree and we have no guarantee
        # that the tree isn't currently rooted within the outgroup clade. There is
        # also no way to identify a node that is guaranteed to be outside the outgroup
        # clade. As such, the tree is randomly rooted on a leaf node not in the outgroup.
        # This random rerooting is performed until the MRCA does not spans all taxa in 
        # the tree.

        leaves_in_tree = sum([1 for _ in new_tree.leaf_node_iter()])
        while True:
            rnd_ingroup_leaf = random.sample(ingroup_in_tree, 1)[0]
            new_tree.reroot_at_edge(rnd_ingroup_leaf.edge,
                                    length1=0.5 * rnd_ingroup_leaf.edge_length,
                                    length2=0.5 * rnd_ingroup_leaf.edge_length)

            mrca = new_tree.mrca(taxa=outgroup_in_tree)
            leaves_in_mrca = sum([1 for _ in mrca.leaf_iter()])
            if leaves_in_mrca != leaves_in_tree:
                break

        if leaves_in_mrca != len(outgroup_in_tree):
            self.logger.info('Outgroup is not monophyletic. Tree will be rerooted at the MRCA of the outgroup.')
            self.logger.info('The outgroup consisted of %d taxa, while the MRCA has %d leaf nodes.' % (len(outgroup_in_tree), leaves_in_mrca))
            if leaves_in_mrca == leaves_in_tree:
                self.logger.warning('The MRCA spans all taxa in the tree.')
                self.logger.warning('This indicating the selected outgroup is likely polyphyletic in the current tree.')
                self.logger.warning('Polyphyletic outgroups are not suitable for rooting. Try another outgroup.')
        else:
            self.logger.info('Outgroup is monophyletic.')

        if mrca.edge_length is None:
            self.logger.info('Tree appears to already be rooted on this outgroup.')
        else:
            self.logger.info('Rerooting tree.')
            new_tree.reroot_at_edge(mrca.edge,
                                length1=0.5 * mrca.edge_length,
                                length2=0.5 * mrca.edge_length)
        
        return new_tree

    def _distribution_plot(self, 
                            rel_dists, 
                            taxa_for_dist_inference, 
                            highlight_polyphyly,
                            highlight_taxa,
                            distribution_table,
                            fmeasure,
                            fmeasure_mono,
                            plot_file):
        """Create plot showing the distribution of taxa at each taxonomic rank.

        Parameters
        ----------
        rel_dists: d[rank_index][taxon] -> relative divergence
            Relative divergence of taxa at each rank.
        taxa_for_dist_inference : iterable
            Taxa to considered when inferring distributions.
        distribution_table : str
            Desired name of output table with distribution information.
        plot_file : str
            Desired name of output plot.
        """

        self.fig.clear()
        self.fig.set_size_inches(12, 6)
        ax = self.fig.add_subplot(111)
        
        # create normal distributions
        for i, rank in enumerate(sorted(rel_dists.keys())):
            v = [dist for taxa, dist in rel_dists[rank].iteritems() if taxa in taxa_for_dist_inference]
            if len(v) < 2:
                continue
                
            u = np_mean(v)
            rv = norm(loc=u, scale=np_std(v))
            x = np_linspace(rv.ppf(0.001), rv.ppf(0.999), 1000)
            nd = rv.pdf(x)
            # ax.plot(x, 0.75 * (nd / max(nd)) + i, 'b-', alpha=0.6, zorder=2)
            # ax.plot((u, u), (i, i + 0.5), 'b-', zorder=2)

        # create percentile and classifciation boundary lines
        percentiles = {}
        for i, rank in enumerate(sorted(rel_dists.keys())):
            v = [dist for taxa, dist in rel_dists[rank].iteritems() if taxa in taxa_for_dist_inference]
            if len(v) == 0:
                continue
                
            p10, p50, p90 = np_percentile(v, [10, 50, 90])
            #ax.plot((p10, p10), (i, i + 0.25), c=(0.3, 0.3, 0.3), lw=2, zorder=2)
            ax.plot((p50, p50), (i, i + 0.5), c=(0.0, 0.0, 1.0), lw=2, zorder=2)
            #ax.plot((p90, p90), (i, i + 0.25), c=(0.3, 0.3, 0.3), lw=2, zorder=2)

            for b in [-0.1, 0.1]:
                boundary = p50 + b
                if boundary < 1.0 and boundary > 0.0:
                    if abs(b) == 0.1:
                        c = (0.0, 0.0, 0.0)
                    else:
                        c = (1.0, 0.0, 0.0)
                    ax.plot((boundary, boundary), (i, i + 0.25), c=c, lw=2, zorder=2)

            percentiles[i] = [p10, p50, p90]

        # create scatter plot and results table
        fout = open(distribution_table, 'w')
        fout.write('Taxa\tRelative Distance\tP10\tMedian\tP90\tPercentile outlier\n')
        x = []
        y = []
        c = []
        labels = []
        rank_labels = []
        for i, rank in enumerate(sorted(rel_dists.keys())):
            rank_label = Taxonomy.rank_labels[rank]
            rank_labels.append(rank_label.capitalize()  + ' (%d)' % len(rel_dists[rank]))
            
            mono = []
            poly = []
            nearly_mono = []
            for clade_label, dist in rel_dists[rank].iteritems():
                x.append(dist)
                y.append(i)
                labels.append(clade_label)

                if ((highlight_polyphyly and fmeasure[clade_label] < fmeasure_mono) or clade_label in highlight_taxa):
                    c.append((1.0,0.0,0.0))
                    poly.append(dist)
                elif (highlight_polyphyly and fmeasure[clade_label] != 1.0):
                    c.append((255.0/255,187.0/255,120.0/255))
                    nearly_mono.append(dist)
                else:
                    c.append((152.0/255,223.0/255,138.0/255))
                    mono.append(dist)
            
                # report results
                v = [clade_label, dist]
                if i in percentiles:
                    p10, p50, p90 = percentiles[i]
                    percentile_outlier = not (dist >= p10 and dist <= p90)
                    v += percentiles[i] + [str(percentile_outlier)]
                else:
                    percentile_outlier = 'Insufficent data to calculate percentiles'
                    v += [-1,-1,-1] + [str(percentile_outlier)]
                
                fout.write('%s\t%.2f\t%.2f\t%.2f\t%.2f\t%s\n' % tuple(v))
        
            # histogram for each rank
            binwidth = 0.025
            bins = np_arange(0, 1.0 + binwidth, binwidth)
            max_bin_count = max(np_histogram(mono + nearly_mono + poly, bins=bins)[0])

            num_taxa = len(mono) + len(poly) + len(nearly_mono)
            if num_taxa == 0:
                break
            
            mono = np_array(mono)
            nearly_mono = np_array(nearly_mono)
            poly = np_array(poly)
            
            bottom_mono = 0
            if len(mono) > 0:
                bottom_mono, b, p = ax.hist(mono, bins=bins,
                          color=(152.0/255,223.0/255,138.0/255),
                          alpha=0.5,
                          weights=0.9 * (1.0 / max_bin_count) * np_ones_like(mono),
                          bottom=i,
                          lw=0,
                          zorder=0)
                      
            bottom_nearly_mono = 0
            if len(nearly_mono) > 0:
                bottom_nearly_mono, b, p = ax.hist(nearly_mono, bins=bins,
                                                      color=(255.0/255,187.0/255,120.0/255),
                                                      alpha=0.5,
                                                      weights=0.9 * (1.0 / max_bin_count) * np_ones_like(nearly_mono),
                                                      bottom=i + bottom_mono,
                                                      lw=0,
                                                      zorder=0)

            if len(poly) > 0:
                ax.hist(poly, bins=bins,
                          color=(1.0, 0.0, 0.0),
                          alpha=0.5,
                          weights=0.9 * (1.0 / max_bin_count) * np_ones_like(poly),
                          bottom=i + bottom_mono + bottom_nearly_mono,
                          lw=0,
                          zorder=0)
        fout.close()

        # overlay scatter plot elements
        scatter = ax.scatter(x, y, alpha=0.5, s=48, c=c, zorder=1)

        # set plot elements
        ax.grid(color=(0.8, 0.8, 0.8), linestyle='dashed')

        ax.set_xlabel('Relative Evolutionary Divergence')
        ax.set_xticks(np_arange(0, 1.05, 0.1))
        ax.set_xlim([-0.05, 1.05])

        ax.set_ylabel('Rank (no. taxa)')
        ax.set_yticks(xrange(0, len(rel_dists)))
        ax.set_ylim([-0.2, len(rel_dists) - 0.01])
        ax.set_yticklabels(rank_labels)

        self.prettify(ax)

        # make plot interactive
        mpld3.plugins.clear(self.fig)
        mpld3.plugins.connect(self.fig, mpld3.plugins.PointLabelTooltip(scatter, labels=labels))
        mpld3.plugins.connect(self.fig, mpld3.plugins.MousePosition(fontsize=10))
        mpld3.save_html(self.fig, plot_file[0:plot_file.rfind('.')] + '.html')

        self.fig.tight_layout(pad=1)
        self.fig.savefig(plot_file, dpi=self.dpi)

    def _median_outlier_file(self, 
                                rel_dists,
                                taxa_for_dist_inference,
                                gtdb_parent_ranks, 
                                output_file):
        """Identify outliers relative to the median of rank distributions.

        Parameters
        ----------
        rel_dists: d[rank_index][taxon] -> relative divergence
            Relative divergence of taxa at each rank.
        taxa_for_dist_inference : iterable
            Taxa to considered when inferring distributions.
        gtdb_parent_ranks: d[taxon] -> string indicating parent taxa
            Parent taxa for each taxon.
        output_file : str
            Desired name of output table.
        """

        # determine median relative distance for each rank
        median_rel_dist = {}
        for rank, d in rel_dists.iteritems():
            v = [dist for taxa, dist in d.iteritems() if taxa in taxa_for_dist_inference]
            if len(v) == 0:
                continue
                
            median_rel_dist[rank] = np_median(v)

        fout = open(output_file, 'w')
        fout.write('Taxa\tGTDB taxonomy\tMedian distance\tMean difference\tClosest rank\tClassification\n')
            
        for i, rank in enumerate(sorted(rel_dists.keys())):
            for clade_label, dist in rel_dists[rank].iteritems():
                if rank in median_rel_dist:
                    delta = dist - median_rel_dist[rank]
                    closest_rank_dist = 1e10
                    for test_rank, test_median in median_rel_dist.iteritems():
                        abs_dist = abs(dist - test_median)
                        if abs_dist < closest_rank_dist:
                            closest_rank_dist = abs_dist
                            closest_rank = Taxonomy.rank_labels[test_rank]

                    classification = "OK"
                    if delta < -0.2:
                        classification = "very overclassified"
                    elif delta < -0.1:
                        classification = "overclassified"
                    elif delta > 0.2:
                        classification = "very underclassified"
                    elif delta > 0.1:
                        classification = "underclassified"

                    fout.write('%s\t%s\t%.3f\t%.3f\t%s\t%s\n' % (clade_label,
                                                                   ';'.join(gtdb_parent_ranks[clade_label]),
                                                                   dist,
                                                                   delta,
                                                                   closest_rank,
                                                                   classification))
                else:
                    fout.write('%s\t%s\t%.3f\t%.3f\t%s\t%s\n' % (clade_label,
                                                                   ';'.join(gtdb_parent_ranks[clade_label]),
                                                                   dist,
                                                                   -1,
                                                                   'NA',
                                                                   'Insufficent data to calcualte median for rank.'))
        fout.close()
        
    def taxa_median_rd(self, phylum_rel_dists):
        """Calculate the median relative divergence for each taxon.
        
        Parameters
        ----------
        phylum_rel_dists: phylum_rel_dists[phylum][rank_index][taxon] -> relative divergences
            Relative divergence of taxon at each rank for different phylum-level rootings.    
        """
        
        medians_for_taxa = defaultdict(lambda: defaultdict(list))
        for p in phylum_rel_dists:
            for rank, d in phylum_rel_dists[p].iteritems():
                for taxon, dist in d.iteritems():
                    medians_for_taxa[rank][taxon].append(dist)
                    
        return medians_for_taxa
        
    def rank_median_rd(self, phylum_rel_dists, taxa_for_dist_inference):
        """Calculate median relative divergence for each rank.
        
        Parameters
        ----------
        phylum_rel_dists: phylum_rel_dists[phylum][rank_index][taxon] -> relative divergences
            Relative divergence of taxon at each rank for different phylum-level rootings.
        taxa_for_dist_inference : iterable
            Taxa to considered when inferring distributions.
        """
        
        medians_for_taxa = self.taxa_median_rd(phylum_rel_dists)
    
        median_for_rank = {}
        for i, rank in enumerate(sorted(medians_for_taxa.keys())):
                v = [np_median(dists) for taxon, dists in medians_for_taxa[rank].iteritems() if taxon in taxa_for_dist_inference]
                
                if v:
                    median_for_rank[rank] = np_median(v)
                
        return median_for_rank
        
    def _distribution_summary_plot(self, 
                                    phylum_rel_dists, 
                                    taxa_for_dist_inference, 
                                    highlight_polyphyly,
                                    highlight_taxa,
                                    fmeasure,
                                    fmeasure_mono,
                                    plot_file):
        """Summary plot showing the distribution of taxa at each taxonomic rank under different rootings.

        Parameters
        ----------
        phylum_rel_dists: phylum_rel_dists[phylum][rank_index][taxon] -> relative divergences
            Relative divergence of taxon at each rank for different phylum-level rootings.
        taxa_for_dist_inference : iterable
            Taxa to considered when inferring distributions.
        plot_file : str
            Desired name of output plot.
        """

        self.fig.clear()
        self.fig.set_size_inches(12, 6)
        ax = self.fig.add_subplot(111)

        # determine median relative distance for each taxa
        medians_for_taxa = self.taxa_median_rd(phylum_rel_dists)

        # create percentile and classification boundary lines
        percentiles = {}
        for i, rank in enumerate(sorted(medians_for_taxa.keys())):
            v = [np_median(dists) for taxon, dists in medians_for_taxa[rank].iteritems() if taxon in taxa_for_dist_inference]
            if not v:
                # not taxa at rank suitable for creating classification boundaries
                continue
            
            p10, p50, p90 = np_percentile(v, [10, 50, 90])
            #ax.plot((p10, p10), (i, i + 0.25), c=(0.3, 0.3, 0.3), lw=2, zorder=2)
            ax.plot((p50, p50), (i, i + 0.5), c=(0.0, 0.0, 1.0), lw=2, zorder=2)
            #ax.plot((p90, p90), (i, i + 0.25), c=(0.3, 0.3, 0.3), lw=2, zorder=2)

            for b in [-0.1, 0.1]:
                boundary = p50 + b
                if boundary < 1.0 and boundary > 0.0:
                    if abs(b) == 0.1:
                        c = (0.0, 0.0, 0.0)
                    else:
                        c = (1.0, 0.0, 0.0)
                    ax.plot((boundary, boundary), (i, i + 0.5), c=c, lw=2, zorder=2)

            percentiles[i] = [p10, p50, p90]

        # create scatter plot and results table
        x = []
        y = []
        c = []
        labels = []
        rank_labels = []
        for i, rank in enumerate(sorted(medians_for_taxa.keys())):
            rank_label = Taxonomy.rank_labels[rank]
            rank_labels.append(rank_label.capitalize() + ' (%d)' % len(medians_for_taxa[rank]))

            mono = []
            poly = []
            near_mono = []
            for clade_label, dists in medians_for_taxa[rank].iteritems():
                md = np_median(dists)
                x.append(md)
                y.append(i)
                labels.append(clade_label)

                if ((highlight_polyphyly and fmeasure[clade_label] < fmeasure_mono) or clade_label in highlight_taxa):
                    c.append((1.0,0.0,0.0))
                    poly.append(md)
                elif (highlight_polyphyly and fmeasure[clade_label] != 1.0):
                    c.append((255.0/255,187.0/255,120.0/255))
                    near_mono.append(md)
                else:
                    c.append((152.0/255,223.0/255,138.0/255))
                    mono.append(md)

            # histogram for each rank
            binwidth = 0.025
            bins = np_arange(0, 1.0 + binwidth, binwidth)
            max_bin_count = max(np_histogram(mono + near_mono + poly, bins=bins)[0])

            mono_bottom = 0
            near_mono_bottom = 0
            mono = np_array(mono)
            near_mono = np_array(near_mono)
            poly = np_array(poly)
            if len(mono) > 0:
                mono_bottom, b, p = ax.hist(mono, bins=bins,
                          color=(152.0/255,223.0/255,138.0/255),
                          alpha=0.5,
                          weights=0.9 * (1.0 / max_bin_count) * np_ones_like(mono),
                          bottom=i,
                          lw=0,
                          zorder=0)

            if len(near_mono) > 0:
                near_mono_bottom, b, p = ax.hist(near_mono, bins=bins,
                                              color=(255.0/255,187.0/255,120.0/255),
                                              alpha=0.5,
                                              weights=0.9 * (1.0 / max_bin_count) * np_ones_like(near_mono),
                                              bottom=i + mono_bottom,
                                              lw=0,
                                              zorder=0)

            if len(poly) > 0:
                ax.hist(poly, bins=bins,
                          color=(1.0, 0.0, 0.0),
                          alpha=0.5,
                          weights=0.9 * (1.0 / max_bin_count) * np_ones_like(poly),
                          bottom=i + mono_bottom + near_mono_bottom,
                          lw=0,
                          zorder=0)
                          
        scatter = ax.scatter(x, y, alpha=0.5, s=48, c=c, zorder=1)

        # set plot elements
        ax.grid(color=(0.8, 0.8, 0.8), linestyle='dashed')

        ax.set_xlabel('Relative Evolutionary Divergence')
        ax.set_xticks(np_arange(0, 1.05, 0.1))
        ax.set_xlim([-0.01, 1.01])

        ax.set_ylabel('Rank (no. taxa)')
        ax.set_yticks(xrange(0, len(medians_for_taxa)))
        ax.set_ylim([-0.2, len(medians_for_taxa) - 0.01])
        ax.set_yticklabels(rank_labels)

        self.prettify(ax)

        # make plot interactive
        mpld3.plugins.clear(self.fig)
        mpld3.plugins.connect(self.fig, mpld3.plugins.PointLabelTooltip(scatter, labels=labels))
        mpld3.plugins.connect(self.fig, mpld3.plugins.MousePosition(fontsize=10))
        mpld3.save_html(self.fig, plot_file[0:plot_file.rfind('.')] + '.html')

        self.fig.tight_layout(pad=1)
        self.fig.savefig(plot_file, dpi=self.dpi)
        self.fig.savefig(plot_file.replace('.png', '.svg'), dpi=self.dpi)

    def _median_summary_outlier_file(self, phylum_rel_dists,
                                            taxa_for_dist_inference,
                                            gtdb_parent_ranks,
                                            outlier_table,
                                            rank_file,
                                            verbose_table):
        """Identify outliers relative to the median of rank distributions.

        Parameters
        ----------
        phylum_rel_dists: phylum_rel_dists[phylum][rank_index][taxon] -> relative divergences
            Relative divergence of taxon at each rank for different phylum-level rootings.
        taxa_for_dist_inference : iterable
            Taxa to considered when inferring distributions.
        gtdb_parent_ranks: d[taxon] -> string indicating parent taxa
            Parent taxa for each taxon.
        outlier_table : str
            Desired name of output table.
        rank_file : str
            Desired name of file indicating median relative distance of each rank.
        verbose_table : boolean
            Print additional columns in output table.
        """
        
        # determine median relative distance for each taxa
        medians_for_taxa = self.taxa_median_rd(phylum_rel_dists)
        
        # determine median relative distance for each rank
        median_for_rank = self.rank_median_rd(phylum_rel_dists, taxa_for_dist_inference)

        fout_rank = open(rank_file, 'w')
        median_str = []
        for rank in sorted(median_for_rank.keys()):
            median_str.append('"' + Taxonomy.rank_labels[rank] + '":' + str(median_for_rank[rank]))
        fout_rank.write('{' + ','.join(median_str) + '}\n')
        fout_rank.close()
            
        fout = open(outlier_table, 'w')
        if verbose_table:
            fout.write('Taxa\tGTDB taxonomy\tMedian distance')
            fout.write('\tMedian of rank\tMedian difference')
            fout.write('\tClosest rank\tClassifciation\n')
        else:
            fout.write('Taxa\tGTDB taxonomy\tMedian distance\tMedian difference\tClosest rank\tClassification\n')
        
        for rank in sorted(median_for_rank.keys()):
            for clade_label, dists in medians_for_taxa[rank].iteritems():
                dists = np_array(dists)

                taxon_median = np_median(dists)
                delta = taxon_median - median_for_rank[rank]

                closest_rank_dist = 1e10
                for test_rank, test_median in median_for_rank.iteritems():
                    abs_dist = abs(taxon_median - test_median)
                    if abs_dist < closest_rank_dist:
                        closest_rank_dist = abs_dist
                        closest_rank = Taxonomy.rank_labels[test_rank]

                classification = "OK"
                if delta < -0.2:
                    classification = "very overclassified"
                elif delta < -0.1:
                    classification = "overclassified"
                elif delta > 0.2:
                    classification = "very underclassified"
                elif delta > 0.1:
                    classification = "underclassified"

                if verbose_table:
                    fout.write('%s\t%s\t%.2f\t%.3f\t%.3f\t%s\t%s\n' % (clade_label,
                                                                       ';'.join(gtdb_parent_ranks[clade_label]),
                                                                       taxon_median,
                                                                       median_for_rank[rank],
                                                                       delta,
                                                                       closest_rank,
                                                                       classification))
                else:
                    fout.write('%s\t%s\t%.3f\t%.3f\t%s\t%s\n' % (clade_label,
                                                                   ';'.join(gtdb_parent_ranks[clade_label]),
                                                                   taxon_median,
                                                                   delta,
                                                                   closest_rank,
                                                                   classification))
        fout.close()

    def rd_fixed_root(self, tree, taxa_for_dist_inference):
        """Scale tree and calculate relative divergence over a single fixed root.
        
        Parameters
        ----------
        tree : Tree
          Dendropy tree.
        taxa_for_dist_inference : set
          Taxa to use for inference relative divergence distributions.
        """
        
        # calculate relative distance to taxa
        rd = RelativeDistance()
        rel_dists = rd.rel_dist_to_named_clades(tree)
        
        # create scaled tree
        rd.decorate_rel_dist(tree)
        for n in tree.preorder_node_iter(lambda n: n != tree.seed_node):
            rd_to_parent = n.rel_dist - n.parent_node.rel_dist
            n.edge_length = rd_to_parent
        
        return rel_dists
        
    def mblet(self, tree, taxa_for_dist_inference):
        """Scale tree and calculate mean branch length to extent taxa.
        
        Parameters
        ----------
        tree : Tree
          Dendropy tree.
        taxa_for_dist_inference : set
          Taxa to use for inference MBLET distributions.
        """
        
        # calculate relative distance to taxa
        rd = RelativeDistance()
        rel_dists = rd.rel_dist_to_named_clades(tree, mblet=True)
        
        # create scaled tree
        rd.decorate_rel_dist(tree)
        for n in tree.preorder_node_iter(lambda n: n != tree.seed_node):
            rd_to_parent = n.rel_dist - n.parent_node.rel_dist
            n.edge_length = rd_to_parent
        
        return rel_dists

    def median_rd_over_phyla(self, 
                                tree, 
                                taxa_for_dist_inference,
                                taxonomy=None):
        """Calculate the median relative divergence over all phyla rootings.
        
        Parameters
        ----------
        tree : Tree
          Dendropy tree.
        taxa_for_dist_inference : set
          Taxa to use for inference relative divergence distributions.
        """
        
        # read taxonomy from tree
        if not taxonomy:
            taxonomy = Taxonomy().read_from_tree(tree, 
                                                    warnings=False)
    
        # get list of phyla level lineages
        all_phyla = get_phyla_lineages(tree)
        self.logger.info('Identified %d phyla.' % len(all_phyla))
        
        phyla = [p for p in all_phyla if p in taxa_for_dist_inference]
        self.logger.info('Using %d phyla as rootings for inferring distributions.' % len(phyla))
        if len(phyla) < 2:
            self.logger.error('Rescaling requires at least 2 valid phyla.')
            sys.exit(-1)
            
        # give each node a unique id
        for i, n in enumerate(tree.preorder_node_iter()):
            n.id = i
    
        # calculate relative divergence for tree rooted on each phylum
        phylum_rel_dists = {}
        rel_node_dists = defaultdict(list)
        rd = RelativeDistance()
        for p in phyla:
            phylum = p.replace('p__', '').replace(' ', '_').lower()
            self.logger.info('Calculating information with rooting on %s.' % phylum.capitalize())
            
            cur_tree = self.root_with_outgroup(tree, taxonomy, p)
            
            # calculate relative distance to taxa
            rel_dists = rd.rel_dist_to_named_clades(cur_tree)
            rel_dists.pop(0, None) # remove results for Domain

            # remove named groups in outgroup
            children = Taxonomy().children(p, taxonomy)
            for r in rel_dists.keys():
                rel_dists[r].pop(p, None)

            for t in children:
                for r in rel_dists.keys():
                    rel_dists[r].pop(t, None)

            phylum_rel_dists[phylum] = rel_dists
            
            # calculate relative distance to all nodes
            rd.decorate_rel_dist(cur_tree)
            
            # determine which lineages represents the 'ingroup'
            ingroup_subtree = None
            for c in cur_tree.seed_node.child_node_iter():
                _support, taxon_name, _auxiliary_info = parse_label(c.label)
                if not taxon_name or p not in taxon_name:
                    ingroup_subtree = c
                    break
            
            # do a preorder traversal of 'ingroup' and record relative divergence to nodes
            for n in ingroup_subtree.preorder_iter():                        
                rel_node_dists[n.id].append(n.rel_dist)
                                                           
        return phylum_rel_dists, rel_node_dists
        
    def _write_rd(self, tree, output_rd_file):
        """Write out relative divergences for each node."""
        
        fout = open(output_rd_file, 'w')
        for n in tree.preorder_node_iter():
            if n.is_leaf():
                fout.write('%s\t%f\n' % (n.taxon.label, n.rel_dist))
            else:
                # get left and right taxa that define this node
                taxa = list(n.preorder_iter(lambda n: n.is_leaf()))
                fout.write('%s|%s\t%f\n' % (taxa[0].taxon.label, taxa[-1].taxon.label, n.rel_dist))
                
        fout.close()
        
    def read_fmeasure(self, fmeasure_table):
        """Read table with F-measure for taxa."""
        
        fmeasure = {}
        with open(fmeasure_table) as f:
            f.readline()
            
            for line in f:
                line_split = line.strip().split('\t')
                
                taxa = line_split[0]
                fmeasure[taxa] = float(line_split[2])
                
        return fmeasure
        
    def run(self, input_tree, 
                    taxonomy_file, 
                    output_dir, 
                    plot_taxa_file,
                    plot_dist_taxa_only,
                    plot_domain,
                    highlight_polyphyly,
                    highlight_taxa_file,
                    trusted_taxa_file,
                    fixed_root,
                    min_children, 
                    min_support,
                    mblet,
                    fmeasure_table,
                    min_fmeasure,
                    fmeasure_mono,
                    verbose_table):
        """Determine distribution of taxa at each taxonomic rank.

        Parameters
        ----------
        input_tree : str
          Name of input tree.
        taxonomy_file : str
          File with taxonomy strings for each taxa.
        output_dir : str
          Desired output directory.
        plot_taxa_file : str
          File specifying taxa to plot. Set to None to consider all taxa.
        plot_dist_taxa_only : boolean    
          Only plot the taxa used to infer distribution.
        plot_domain : boolean
          Plot domain rank.
        trusted_taxa_file : str
          File specifying trusted taxa to consider when inferring distribution. Set to None to consider all taxa.
        fixed_root : boolean
          Usa a single fixed root to infer outliers.
        min_children : int
          Only consider taxa with at least the specified number of children taxa when inferring distribution.
        min_support : float
          Only consider taxa with at least this level of support when inferring distribution.
        verbose_table : boolean
          Print additional columns in output table.
        """
        
        # read tree
        self.logger.info('Reading tree.')
        tree = dendropy.Tree.get_from_path(input_tree, 
                                            schema='newick', 
                                            rooting='force-rooted', 
                                            preserve_underscores=True)

        input_tree_name = os.path.splitext(os.path.basename(input_tree))[0]

        # pull taxonomy from tree and file
        self.logger.info('Reading taxonomy.')
        taxonomy = Taxonomy().read(taxonomy_file)
        tree_taxonomy = Taxonomy().read_from_tree(input_tree,
                                                    warnings=False)
            
        gtdb_parent_ranks = Taxonomy().parents(tree_taxonomy)

        # read trusted taxa
        trusted_taxa = None
        if trusted_taxa_file:
            trusted_taxa = read_taxa_file(trusted_taxa_file)
            
        # read F-measure for taxa
        fmeasure = None
        if fmeasure_table:
            fmeasure = self.read_fmeasure(fmeasure_table)

        # determine taxa to be used for inferring distribution
        taxa_for_dist_inference = filter_taxa_for_dist_inference(tree, 
                                                                    taxonomy, 
                                                                    trusted_taxa, 
                                                                    min_children, 
                                                                    min_support,
                                                                    fmeasure,
                                                                    min_fmeasure)

        # limit plotted taxa
        taxa_to_plot = None
        if plot_dist_taxa_only:
            taxa_to_plot = taxa_for_dist_inference
        elif plot_taxa_file:
            taxa_to_plot = read_taxa_file(plot_taxa_file)
        else:
            # plot every taxon defined in tree
            taxa_to_plot = set()
            for node in tree.preorder_node_iter():
                support, taxon, _auxiliary_info = parse_label(node.label)
                if taxon:
                    taxon = taxon.split(';')[-1].strip() # get most specific taxon from compound names 
                                                         # (e.g. p__Armatimonadetes; c__Chthonomonadetes)
                    taxa_to_plot.add(taxon)
            
            if False:
                # HACK FOR NCBI: only plot taxa with >= 2 taxa
                taxa_to_plot = set()
                for node in tree.preorder_node_iter():
                    if not node.label or node.is_leaf():
                        continue

                    support, taxon, _auxiliary_info = parse_label(node.label)
                    if not taxon:
                        continue
                    taxon = taxon.split(';')[-1].strip() # get most specific taxon from compound names 
                                                         # (e.g. p__Armatimonadetes; c__Chthonomonadetes)
                  
                    # count number of subordinate children
                    rank_prefix = taxon[0:3]
                    if min_children > 0 and rank_prefix != 's__':
                        child_rank_index = Taxonomy().rank_index[rank_prefix] + 1
                        child_rank_prefix = Taxonomy.rank_prefixes[child_rank_index]
                        subordinate_taxa = set()
                        for leaf in node.leaf_iter():
                            taxa = taxonomy.get(leaf.taxon.label, Taxonomy.rank_prefixes)
                            if len(taxa) > child_rank_index:
                                sub_taxon = taxa[child_rank_index]
                                if sub_taxon != Taxonomy.rank_prefixes[child_rank_index] and sub_taxon.startswith(child_rank_prefix):
                                    subordinate_taxa.add(sub_taxon)

                        if len(subordinate_taxa) < min_children:
                            continue
                            
                    taxa_to_plot.add(taxon)
            
        # highlight taxa
        highlight_taxa = set()
        if highlight_taxa_file:
            for line in open(highlight_taxa_file):
                highlight_taxa.add(line.strip().split('\t')[0])
                
        # check if a single fixed root should be used
        if fixed_root or mblet:
            self.logger.info('Using single fixed rooting for inferring distributions.')
            if not mblet:
                rel_dists = self.rd_fixed_root(tree, taxa_for_dist_inference)
            else:
                rel_dists = self.mblet(tree, taxa_for_dist_inference)
                
            # create fixed rooting style tables and plots
            distribution_table = os.path.join(output_dir, '%s.rank_distribution.tsv' % input_tree_name)
            plot_file = os.path.join(output_dir, '%s.png' % input_tree_name)
            self._distribution_plot(rel_dists, 
                                        taxa_for_dist_inference,
                                        highlight_polyphyly,
                                        highlight_taxa,
                                        distribution_table,
                                        fmeasure,
                                        fmeasure_mono,
                                        plot_file)

            median_outlier_table = os.path.join(output_dir, '%s.tsv' % input_tree_name)
            self._median_outlier_file(rel_dists, 
                                        taxa_for_dist_inference, 
                                        gtdb_parent_ranks, 
                                        median_outlier_table)
        else:
            # calculate relative distance to taxa
            rd = RelativeDistance()
            rel_dists = rd.rel_dist_to_named_clades(tree)
            
            # restrict to taxa of interest
            if taxa_to_plot:
                for r in rel_dists:
                    for k in set(rel_dists[r].keys()) - set(taxa_to_plot):
                        del rel_dists[r][k]
            
            # report number of taxa at each rank
            print ''
            print 'Rank\tTaxa to Plot\tTaxa for Inference'
            for rank, taxa in rel_dists.iteritems():
                taxa_for_inference = [x for x in taxa if x in taxa_for_dist_inference]
                print '%s\t%d\t%d' % (Taxonomy.rank_labels[rank], len(taxa), len(taxa_for_inference))
            print ''
        
            # *** determine phyla for inferring distribution
            if True:
                phylum_rel_dists, rel_node_dists = self.median_rd_over_phyla(tree, 
                                                                                taxa_for_dist_inference)
            else:                                                                    
                phyla_for_inference = filter_taxa_for_dist_inference(tree, 
                                                                        taxonomy, 
                                                                        trusted_taxa, 
                                                                        2, 
                                                                        min_support,
                                                                        fmeasure,
                                                                        min_fmeasure)
                phylum_rel_dists, rel_node_dists = self.median_rd_over_phyla(tree, 
                                                                                phyla_for_inference)
                print ''
                print 'Phyla for RED Inference:'
                print ','.join(phylum_rel_dists)
                phyla_file = os.path.join(output_dir, '%s.phyla.tsv' % input_tree_name)
                fout = open(phyla_file, 'w')
                for p in phylum_rel_dists:
                    fout.write(p + '\n')
                fout.close()
                                                                            
            # set edge lengths to median value over all rootings
            tree.seed_node.rel_dist = 0.0
            for n in tree.preorder_node_iter(lambda n: n != tree.seed_node):
                n.rel_dist = np_median(rel_node_dists[n.id])
                rd_to_parent = n.rel_dist - n.parent_node.rel_dist
                if rd_to_parent < 0:
                    self.logger.warning('Not all branches are positive after scaling.')
                n.edge_length = rd_to_parent

            for phylum, rel_dists in phylum_rel_dists.iteritems():
                phylum_dir = os.path.join(output_dir, phylum)
                if not os.path.exists(phylum_dir):
                    os.makedirs(phylum_dir)
                    
                # restrict to taxa of interest
                if taxa_to_plot:
                    for r in rel_dists:
                        for k in set(rel_dists[r].keys()) - set(taxa_to_plot):
                            del rel_dists[r][k]
                    
                # create distribution plot
                distribution_table = os.path.join(phylum_dir, '%s.rank_distribution.tsv' % phylum)
                plot_file = os.path.join(phylum_dir, '%s.rank_distribution.png' % phylum)
                self._distribution_plot(rel_dists, 
                                        taxa_for_dist_inference,
                                        highlight_polyphyly,
                                        highlight_taxa,
                                        distribution_table,
                                        fmeasure,
                                        fmeasure_mono,
                                        plot_file)

                median_outlier_table = os.path.join(phylum_dir, '%s.median_outlier.tsv' % phylum)
                self._median_outlier_file(rel_dists, 
                                            taxa_for_dist_inference, 
                                            gtdb_parent_ranks,
                                            median_outlier_table)
   
            plot_file = os.path.join(output_dir, '%s.png' % input_tree_name)
            self._distribution_summary_plot(phylum_rel_dists, 
                                            taxa_for_dist_inference,
                                            highlight_polyphyly,
                                            highlight_taxa,
                                            fmeasure,
                                            fmeasure_mono,
                                            plot_file)

            median_outlier_table = os.path.join(output_dir, '%s.tsv' % input_tree_name)
            median_rank_file = os.path.join(output_dir, '%s.dict' % input_tree_name)
            self._median_summary_outlier_file(phylum_rel_dists, 
                                                taxa_for_dist_inference, 
                                                gtdb_parent_ranks, 
                                                median_outlier_table, 
                                                median_rank_file, 
                                                verbose_table)

        output_rd_file = os.path.join(output_dir, '%s.node_rd.tsv' % input_tree_name)
        self._write_rd(tree, output_rd_file)
                                                
        output_tree = os.path.join(output_dir, '%s.scaled.tree' % input_tree_name)
        tree.write_to_path(output_tree, 
                            schema='newick', 
                            suppress_rooting=True, 
                            unquoted_underscores=True)                
