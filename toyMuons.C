#include <iostream>
#include <vector>
#include <cmath>
#include <TMath.h>
#include <TH2F.h>
#include <TCanvas.h>
#include <TRandom3.h>
#include <TLine.h>
#include <TBox.h>
#include <TMarker.h>
#include <TH1F.h>
#include <TLegend.h>
#include <TStyle.h>
#include <TString.h>
#include <TTree.h>
#include <TFile.h>

// Bar index hit by coordinate x, or -1 if x falls in a gap or outside the layer.
static int barIndex(double x, int nBars, double barW, double gap)
{
    const double pitch = barW + gap;
    const double Lx = nBars * barW + (nBars - 1) * gap;
    const double xl = x + Lx / 2.0;
    if (xl < 0.0 || xl > Lx) return -1;
    int i = static_cast<int>(xl / pitch);
    if (i >= nBars) i = nBars - 1;
    // inside bar i?  bar i spans [i*pitch, i*pitch + barW]
    if (xl - i * pitch > barW) return -1;
    return i;
}

void toyMuons()
{
    // geometry & simulation parameters (cm, s)
    const double I0 = 1.0;            // muon flux [muons / (cm^2 * s)]
    const double exposure_s = 3600.0;    // exposure time [s]
    const int    nBars = 4;           // bars per layer
    const double barW = 4.0;          // bar width along X [cm]
    const double gap = 0.0;           // gap between neighbouring bars [cm]
    const double Ly = 20.0;           // bar length along Y [cm]
    const double h = 15.0;            // distance top layer -> bottom layer [cm]
    const int    nPrint = 20;         // print first N coincidence events
    const int    nDraw = 100;         // draw first N coincidence tracks

    const double Lx = nBars * barW + (nBars - 1) * gap;  // layer width [cm]
    const double A = Lx * Ly;                            // generation area [cm^2]
    const int num_muons = static_cast<int>(I0 * A * exposure_s);

    TRandom3 randGen(0);

    // output tree: one entry per muon crossing the top layer plane
    int topMask = 0, botMask = 0;   // bit i set = bar i fired
    int topBar = -1, botBar = -1;
    bool coinc = false;
    double x0, y0, xb, yb, th, ph;
    TTree *tree = new TTree("events", "toy MC bar hits");
    tree->Branch("x0", &x0);
    tree->Branch("y0", &y0);
    tree->Branch("xb", &xb);
    tree->Branch("yb", &yb);
    tree->Branch("theta", &th);
    tree->Branch("phi", &ph);
    tree->Branch("topMask", &topMask);
    tree->Branch("botMask", &botMask);
    tree->Branch("topBar", &topBar);
    tree->Branch("botBar", &botBar);
    tree->Branch("coinc", &coinc);

    TH2F *hMat = new TH2F("hMat", "Coincidences;Top bar;Bottom bar",
                          nBars, 0, nBars, nBars, 0, nBars);
    hMat->SetDirectory(0);

    // projected angle in the XZ plane: atan((xb - x0) / h), signed
    TH1F *hAng = new TH1F("hAng", "XZ projected angle;#theta_{XZ} [deg];Muons",
                          90, -90, 90);
    TH1F *hAngC = new TH1F("hAngC", "coincidences", 90, -90, 90);
    TH1F *hAngB = new TH1F("hAngB", "sampled from coincidences", 90, -90, 90);
    hAng->SetDirectory(0);
    hAngC->SetDirectory(0);
    hAngB->SetDirectory(0);

    struct Track { double x0, y0, xb, yb; };
    std::vector<Track> tracks;

    int nCoinc = 0;
    std::cout << "Muons to generate: " << num_muons << std::endl;

    for (int i = 0; i < num_muons; ++i)
    {
        // direction: cos^2 zenith distribution (same as the template)
        double u = randGen.Uniform(0.0, 1.0);
        th = TMath::ACos(TMath::Sqrt(u));
        ph = randGen.Uniform(0.0, 2.0 * TMath::Pi());

        // entrance point on the top layer plane (z = h)
        x0 = randGen.Uniform(-Lx / 2.0, Lx / 2.0);
        y0 = randGen.Uniform(-Ly / 2.0, Ly / 2.0);

        // point on the bottom layer plane (z = 0)
        xb = x0 + h * TMath::Tan(th) * TMath::Cos(ph);
        yb = y0 + h * TMath::Tan(th) * TMath::Sin(ph);

        // bar hits
        topBar = barIndex(x0, nBars, barW, gap);
        botBar = (std::abs(yb) <= Ly / 2.0) ? barIndex(xb, nBars, barW, gap) : -1;
        topMask = (topBar >= 0) ? (1 << topBar) : 0;
        botMask = (botBar >= 0) ? (1 << botBar) : 0;
        coinc = (topMask != 0 && botMask != 0);

        tree->Fill();

        const double thXZ = TMath::ATan((xb - x0) / h) * TMath::RadToDeg();
        hAng->Fill(thXZ);
        if (coinc) hAngC->Fill(thXZ);

        if (coinc)
        {
            ++nCoinc;
            hMat->Fill(topBar + 0.5, botBar + 0.5);
            if ((int)tracks.size() < nDraw) tracks.push_back({x0, y0, xb, yb});

            // XZ angle from bar centers: line from top bar center to bottom bar center
            const double xcTop = -Lx / 2.0 + topBar * (barW + gap) + barW / 2.0;
            const double xcBot = -Lx / 2.0 + botBar * (barW + gap) + barW / 2.0;
            const double thBars = TMath::ATan((xcBot - xcTop) / h) * TMath::RadToDeg();
            
            if (nCoinc <= nPrint)
            {
                std::cout << "evt " << i << "  top: ";
                for (int b = 0; b < nBars; ++b) std::cout << ((topMask >> b) & 1);
                std::cout << "  bottom: ";
                for (int b = 0; b < nBars; ++b) std::cout << ((botMask >> b) & 1);
                std::cout << "  -> T" << topBar << " & B" << botBar
                          << Form("  theta_XZ(bars) = %+.1f deg, theta_XZ(true) = %+.1f deg",
                                  thBars, thXZ) << std::endl;
            }
            hAngB->Fill(thBars);
        }
    }

    std::cout << "Coincidences: " << nCoinc << " / " << num_muons
              << Form("  (%.2f %%)", 100.0 * nCoinc / num_muons) << std::endl;

    // save tree
    TFile *fout = TFile::Open("L0bars.root", "RECREATE");
    tree->Write();
    hMat->Write();
    fout->Close();

    // display
    gStyle->SetOptStat(0);
    TCanvas *c = new TCanvas("c", "Bar toy MC", 1200, 800);
    c->Divide(2, 1);

    // left: coincidence matrix
    c->cd(1);
    hMat->SetTitle(Form("Coincidences | %d s | %d / %d muons", (int)exposure_s, nCoinc, num_muons));
    hMat->GetXaxis()->SetNdivisions(nBars);
    hMat->GetYaxis()->SetNdivisions(nBars);
    hMat->Draw("colz text");

    // right: XZ view with bars and sample tracks
    c->cd(2);
    gPad->Divide(1, 2);
    c->cd(2)->cd(1);
    TH2F *frame = new TH2F("frame", "XZ view;X [cm];Z [cm]", 10, -Lx / 2 - 20, Lx / 2 + 20,
                           10, -10, h + 10);
    frame->Draw();
    gPad->SetGrid();
    const double half_th = 0.5;
    for (int b = 0; b < nBars; ++b)
    {
        double xl = -Lx / 2.0 + b * (barW + gap);
        TBox *tb = new TBox(xl, h - half_th, xl + barW, h + half_th);
        tb->SetFillStyle(0);
        tb->SetLineColor(kBlue + 2);
        tb->SetLineWidth(2);
        tb->Draw("same");
        TBox *bb = new TBox(xl, -half_th, xl + barW, half_th);
        bb->SetFillStyle(0);
        bb->SetLineColor(kRed + 1);
        bb->SetLineWidth(2);
        bb->Draw("same");
    }
    for (const auto &t : tracks)
    {
        TLine *l = new TLine(t.x0, h, t.xb, 0.0);
        l->SetLineColor(kCyan + 1);
        l->Draw("same");
    }

    // right-bottom: projected angle, all muons vs coincidences
    c->cd(2)->cd(2);
    hAng->SetLineColor(kBlue + 2);
    hAngC->SetLineColor(kRed + 1);
    hAngB->SetLineColor(kCyan + 3);

    hAng->Scale(1.0 / hAng->GetMaximum());
    hAngC->Scale(1.0 / hAngC->GetMaximum());
    hAngB->Scale(1.0 / hAngB->GetMaximum());

    hAng->Draw("hist");
    hAngC->Draw("hist same");
    hAngB->Draw("hist same");

    TLegend *leg = new TLegend(0.65, 0.75, 0.92, 0.92);
    leg->AddEntry(hAng, "All muons on top layer", "l");
    leg->AddEntry(hAngC, "Top & bottom coincidence", "l");
    leg->AddEntry(hAngB, "Top & bottom coincidence - sampled angle", "l");
    leg->Draw();

    c->Update();
}